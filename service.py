#!/usr/bin/env python3
"""Create a new GitOps service values file (and optional kubectl Ingress YAML)."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple


APP_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = APP_ROOT.parent
APPLICATIONS_DIR = APP_ROOT / "applications"
PROXY_INGRESS_DIR = WORKSPACE_ROOT / "system" / "proxy_ingress"

KUBESEAL_CONTROLLER_NAME = "sealed-secret-sealed-secrets"
KUBESEAL_CONTROLLER_NAMESPACE = "default"


def parse_image(image: str) -> Tuple[str, str]:
    if ":" not in image:
        return image, "latest"
    repository, tag = image.rsplit(":", 1)
    if not repository or not tag:
        raise ValueError(f"invalid --image value: {image}")
    return repository, tag


def yaml_quote(value: str) -> str:
    if value == "":
        return '""'
    special = any(ch in value for ch in ":#{}[],&*?|-<>=!%@`'\\") or value[0].isspace()
    if special or value.lower() in {"true", "false", "null", "yes", "no"}:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def render_values(
    name: str,
    repository: str,
    tag: str,
    port: int,
    target_port: int,
    replicas: int,
    pvc_size: Optional[str],
    storage_class: str,
    mount_path: str,
    secret_enabled: bool,
    secret_name: str,
    encrypted_data: Dict[str, str],
) -> str:
    lines = [
        f"nameOverride: {name}",
        "",
        f"replicaCount: {replicas}",
        "revisionHistoryLimit: 3",
        "",
        "image:",
        f"  repository: {repository}",
        f'  tag: "{tag}"',
        "  pullPolicy: IfNotPresent",
        "",
        "service:",
        "  type: ClusterIP",
        f"  port: {port}",
        f"  targetPort: {target_port}",
        "",
        "persistence:",
        f"  enabled: {'true' if pvc_size else 'false'}",
    ]
    if pvc_size:
        lines.extend(
            [
                f"  storageClass: {yaml_quote(storage_class)}",
                "  accessModes:",
                "    - ReadWriteOnce",
                f"  size: {pvc_size}",
                f"  mountPath: {mount_path}",
            ]
        )
    lines.extend(
        [
            "",
            "secret:",
            f"  enabled: {'true' if secret_enabled else 'false'}",
        ]
    )
    if secret_enabled:
        lines.append(f"  name: {secret_name}")
        lines.append("  encryptedData:")
        if encrypted_data:
            for key, value in encrypted_data.items():
                lines.append(f"    {key}: {value}")
        else:
            lines.append("    {}")
    lines.append("")
    return "\n".join(lines)


def default_ingress_path(component: str) -> str:
    if component in {"back", "backend", "api"}:
        return "/api"
    return "/"


def render_ingress(
    ingress_name: str,
    service_name: str,
    namespace: str,
    host: str,
    port: int,
    path: str,
    tls: bool,
) -> str:
    annotations = ""
    tls_block = ""
    if tls:
        annotations = (
            "  annotations:\n"
            "    cert-manager.io/cluster-issuer: letsencrypt-prod\n"
        )
        tls_block = (
            "  tls:\n"
            "  - hosts:\n"
            f"      - {host}\n"
            "    secretName: cert-manager-prod\n"
        )
    return (
        "apiVersion: networking.k8s.io/v1\n"
        "kind: Ingress\n"
        "metadata:\n"
        f"  name: {ingress_name}\n"
        f"  namespace: {namespace}\n"
        f"{annotations}"
        "spec:\n"
        "  ingressClassName: traefik\n"
        f"{tls_block}"
        "  rules:\n"
        f"  - host: {host}\n"
        "    http:\n"
        "      paths:\n"
        f"      - path: {path}\n"
        "        pathType: Prefix\n"
        "        backend:\n"
        "          service:\n"
        f"            name: {service_name}\n"
        "            port:\n"
        f"              number: {port}\n"
    )


def render_certificate(namespace: str, host: str) -> str:
    return (
        "apiVersion: cert-manager.io/v1\n"
        "kind: Certificate\n"
        "metadata:\n"
        "  name: cert-manager-prod\n"
        f"  namespace: {namespace}\n"
        "spec:\n"
        "  secretName: cert-manager-prod\n"
        "  issuerRef:\n"
        "    name: letsencrypt-prod\n"
        "    kind: ClusterIssuer\n"
        "  dnsNames:\n"
        f"    - {host}\n"
    )


def parse_secret_file(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid secret line (expected KEY=VALUE): {raw_line}")
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    if not data:
        raise ValueError(f"no KEY=VALUE entries in {path}")
    return data


def prompt_secrets() -> Dict[str, str]:
    print("Enter secrets as KEY=VALUE. Empty line to finish.")
    data: Dict[str, str] = {}
    while True:
        try:
            line = input("secret> ").strip()
        except EOFError:
            break
        if not line:
            break
        if "=" not in line:
            print("expected KEY=VALUE", file=sys.stderr)
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def kubeseal_encrypt(
    name: str,
    namespace: str,
    plaintext: Dict[str, str],
) -> Dict[str, str]:
    kubeseal = shutil.which("kubeseal")
    if not kubeseal:
        print(
            "kubeseal not found. Filling empty encryptedData. "
            f"Seal later with: kubeseal --controller-name={KUBESEAL_CONTROLLER_NAME} "
            f"--controller-namespace={KUBESEAL_CONTROLLER_NAMESPACE} --format yaml",
            file=sys.stderr,
        )
        return {}

    secret_lines = [
        "apiVersion: v1",
        "kind: Secret",
        "metadata:",
        f"  name: {name}",
        f"  namespace: {namespace}",
        "type: Opaque",
        "stringData:",
    ]
    for key, value in plaintext.items():
        secret_lines.append(f"  {key}: {yaml_quote(value)}")
    secret_yaml = "\n".join(secret_lines) + "\n"

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as tmp:
        tmp.write(secret_yaml)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            [
                kubeseal,
                "--controller-name",
                KUBESEAL_CONTROLLER_NAME,
                "--controller-namespace",
                KUBESEAL_CONTROLLER_NAMESPACE,
                "--format",
                "yaml",
                "--secret-file",
                tmp_path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            # Older kubeseal reads Secret YAML from stdin.
            result = subprocess.run(
                [
                    kubeseal,
                    "--controller-name",
                    KUBESEAL_CONTROLLER_NAME,
                    "--controller-namespace",
                    KUBESEAL_CONTROLLER_NAMESPACE,
                    "--format",
                    "yaml",
                ],
                input=secret_yaml,
                capture_output=True,
                text=True,
                check=False,
            )
        if result.returncode != 0:
            print(
                "kubeseal failed; leaving encryptedData empty.\n"
                f"{result.stderr.strip()}",
                file=sys.stderr,
            )
            return {}
        return extract_encrypted_data(result.stdout)
    finally:
        os.unlink(tmp_path)


def extract_encrypted_data(sealed_yaml: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    in_block = False
    for line in sealed_yaml.splitlines():
        if line.startswith("  encryptedData:"):
            in_block = True
            continue
        if in_block:
            if line.startswith("  ") and not line.startswith("    "):
                break
            stripped = line.strip()
            if not stripped or stripped == "{}":
                continue
            if ":" not in stripped:
                continue
            key, value = stripped.split(":", 1)
            data[key.strip()] = value.strip()
    return data


def cmd_create(args: argparse.Namespace) -> int:
    app_name = args.name
    component = (args.component or "").strip()
    workload_name = component if component else app_name
    namespace = f"{app_name}-system"
    if component:
        secret_name = f"{app_name}-{component}-secrets"
        values_path = APPLICATIONS_DIR / app_name / component / "values.yaml"
        ingress_filename = f"traefik-ingress-{component}.yaml"
        ingress_name = f"{app_name}-{component}-ingress"
    else:
        secret_name = f"{app_name}-secrets"
        values_path = APPLICATIONS_DIR / app_name / "values.yaml"
        ingress_filename = "traefik-ingress.yaml"
        ingress_name = f"{app_name}-ingress"
    ingress_path = args.ingress_path or default_ingress_path(component)

    if values_path.exists() and not args.force:
        print(f"already exists: {values_path} (use --force to overwrite)", file=sys.stderr)
        return 1

    try:
        repository, tag = parse_image(args.image)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    encrypted_data: Dict[str, str] = {}
    if args.secret:
        plaintext: Dict[str, str] = {}
        if args.secret_file:
            plaintext = parse_secret_file(Path(args.secret_file))
        elif sys.stdin.isatty():
            plaintext = prompt_secrets()
        if plaintext:
            encrypted_data = kubeseal_encrypt(secret_name, namespace, plaintext)
        elif not args.secret_file:
            print(
                "secret enabled with empty encryptedData. "
                "Pass --secret-file or run kubeseal using the existing controller.",
                file=sys.stderr,
            )

    values_path.parent.mkdir(parents=True, exist_ok=True)
    values_path.write_text(
        render_values(
            name=workload_name,
            repository=repository,
            tag=tag,
            port=args.port,
            target_port=args.target_port or args.port,
            replicas=args.replicas,
            pvc_size=args.pvc or None,
            storage_class=args.storage_class,
            mount_path=args.mount_path,
            secret_enabled=bool(args.secret),
            secret_name=secret_name,
            encrypted_data=encrypted_data,
        ),
        encoding="utf-8",
    )
    print(f"wrote {values_path}")

    if args.host:
        if not PROXY_INGRESS_DIR.parent.exists():
            print(
                f"system/ directory not found at {PROXY_INGRESS_DIR.parent}. "
                "Skipping kubectl Ingress YAML.",
                file=sys.stderr,
            )
        else:
            ingress_dir = PROXY_INGRESS_DIR / app_name
            ingress_dir.mkdir(parents=True, exist_ok=True)
            ingress_file = ingress_dir / ingress_filename
            ingress_file.write_text(
                render_ingress(
                    ingress_name=ingress_name,
                    service_name=workload_name,
                    namespace=namespace,
                    host=args.host,
                    port=args.port,
                    path=ingress_path,
                    tls=args.tls,
                ),
                encoding="utf-8",
            )
            print(f"wrote {ingress_file}")
            if args.tls:
                cert_path = ingress_dir / "certificate.yaml"
                cert_path.write_text(
                    render_certificate(namespace, args.host),
                    encoding="utf-8",
                )
                print(f"wrote {cert_path}")
            print(
                f"apply Ingress with: kubectl apply -f {ingress_dir}"
            )

    print(
        "push the applications/ values.yaml to localInfra.git for ApplicationSet. "
        "Ingress is not managed by Helm; apply the kubectl YAML separately."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate GitOps values.yaml for a new service"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create applications/<name>/values.yaml")
    create.add_argument("--name", required=True, help="app name; namespace is <name>-system")
    create.add_argument(
        "--component",
        default="",
        help="workload name (front, back, worker, ...). writes applications/<name>/<component>/ and shares namespace",
    )
    create.add_argument("--image", required=True, help="repository:tag")
    create.add_argument("--port", type=int, required=True, help="Service port")
    create.add_argument("--target-port", type=int, default=0, help="container port (defaults to --port)")
    create.add_argument("--replicas", type=int, default=1)
    create.add_argument("--host", default="", help="Ingress host; writes system/proxy_ingress/<name>/")
    create.add_argument(
        "--ingress-path",
        default="",
        help="Ingress path. default / , or /api when --component is back/backend/api",
    )
    create.add_argument("--tls", action="store_true", help="add cert-manager TLS to the kubectl Ingress YAML")
    create.add_argument("--pvc", default="", help="PVC size, e.g. 20Gi")
    create.add_argument("--storage-class", default="", help="StorageClass name (empty = cluster default)")
    create.add_argument("--mount-path", default="/data")
    create.add_argument("--secret", action="store_true", help="enable SealedSecret")
    create.add_argument("--secret-file", default="", help="KEY=VALUE file to seal with kubeseal")
    create.add_argument("--force", action="store_true")
    create.set_defaults(func=cmd_create)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
