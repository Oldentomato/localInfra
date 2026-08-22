# 신규 서비스 추가

기존 앱(`portfolio`, `tomato_board` 등)은 이 절차를 쓰지 않는다. 기존 CI/CD, Helm 차트, kubectl Ingress는 그대로 둔다.

신규 서비스만 `applications/` + 공통 차트 `charts/service` + ApplicationSet을 사용한다.

Ingress는 Helm이 만들지 않는다. 지금처럼 `system/proxy_ingress/` YAML을 `kubectl apply` 한다.

## 한 번만 하는 작업

이 k3s 클러스터에 **최초 1회**만 하면 된다. 서비스를 추가할 때마다 반복하지 않는다. Argo CD, Sealed Secrets를 설치했던 것과 같다.

적용하는 파일은 두 개다.

- `system/argoCD/services/appProject.yaml` → 공통 AppProject `services-project`
- `system/argoCD/services/applicationset.yaml` → `applications/` 폴더를 보는 감시자 (ApplicationSet)

```powershell
kubectl apply -f ../system/argoCD/services/
```

(`k3s_infra` 루트에서는 `kubectl apply -f system/argoCD/services/`)

이 명령은 서비스를 배포하지 않는다. Argo CD에게 “앞으로 GitHub `localInfra`의 `applications/` 를 지켜봐라”고 등록할 뿐이다.

- `applications/`가 **비어 있어도 된다.** Application이 0개인 상태로 기다리며, 에러가 아니다.
- 디스크에 `system/argoCD/<서비스>/applications/*.yaml` 을 만들어 주지 않는다. Argo CD **안에** Application 리소스를 직접 생성한다.
- 기존처럼 `portfolio`, `post_blog`용 `appProject.yaml` / `applications/*.yaml`을 신규 서비스마다 만들 필요 없다.

Git에 values.yaml이 생기면 ApplicationSet이 Argo CD Application을 자동 생성한다. `--component` 값은 정해진 목록이 아니다. 폴더 이름이 곧 컴포넌트 이름이다.

| Git 경로 | Application 이름 | Namespace |
| --- | --- | --- |
| `applications/order-agent/values.yaml` | `order-agent` | `order-agent-system` |
| `applications/order-agent/front/values.yaml` | `order-agent-front` | `order-agent-system` |
| `applications/order-agent/back/values.yaml` | `order-agent-back` | `order-agent-system` |
| `applications/order-agent/worker/values.yaml` | `order-agent-worker` | `order-agent-system` |

같은 `--name` 아래 컴포넌트는 **같은 namespace**다. AppProject는 `services-project` 하나다.

ApplicationSet YAML을 바꾼 뒤에는 다시 `kubectl apply` 해야 한다.

## 첫 배포 순서 (GitOps vs GitHub Actions)

앱 레포 GitHub Actions가 **이미지 push**와 **GitOps `image.tag` 변경**을 같이 하므로, 둘 다 서로를 필요로 한다.

- GitOps values 파일이 없으면 → Actions의 tag 변경 단계가 실패한다.
- Docker Hub에 이미지가 없으면 → Argo CD가 Pod를 띄워도 `ImagePullBackOff`가 난다.

그래서 **파일을 먼저 만들고, 이미지는 그다음에 올린다.**

1. GitOps에 values.yaml을 만든다. `--image`의 tag는 곧 Actions가 찍을 첫 버전과 맞춘다.
2. 그 파일을 localInfra.git `main`에 push한다.
3. 앱 레포 GitHub Actions의 `GITOPS_VALUES_PATH`를 그 파일로 맞춘다. (키는 기존처럼 `image.tag`)
4. 앱 레포에서 Actions를 돌린다.
5. Ingress는 이미지가 올라온 뒤 `kubectl apply` 한다.

Actions를 GitOps 파일보다 먼저 돌리지 않는다.

## 서비스 추가

### 단일 서비스

```powershell
cd app
python service.py create `
  --name order-agent `
  --image oldentomato/order-agent:1.0.0 `
  --port 8080 `
  --host order-agent.oldensystem.co.kr `
  --tls
```

- values: `applications/order-agent/values.yaml`
- namespace: `order-agent-system`
- Actions 경로: `applications/order-agent/values.yaml`

### 한 앱에 컴포넌트 여러 개 (같은 namespace)

`--name`은 앱 이름이고, `--component`는 그 안의 워크로드 이름이다. `front`/`back` 말고 `worker`, `admin`, `bot` 등도 된다.

```powershell
cd app
python service.py create --name order-agent --component worker --image oldentomato/order-worker:sha-pending --port 8081
```

tomato_board처럼 front + back 예시:

```powershell
python service.py create `
  --name order-agent `
  --component front `
  --image oldentomato/order-web:sha-pending `
  --port 3000 `
  --host order-agent.oldensystem.co.kr `
  --tls

python service.py create `
  --name order-agent `
  --component back `
  --image oldentomato/order-api:sha-pending `
  --port 8080 `
  --host order-agent.oldensystem.co.kr `
  --tls `
  --secret `
  --secret-file .\back.env
```

생성되는 것:

| 파일 | 역할 |
| --- | --- |
| `applications/order-agent/front/values.yaml` | front GitOps. Service 이름은 `front` |
| `applications/order-agent/back/values.yaml` | back GitOps. Service 이름은 `back` |
| `system/proxy_ingress/order-agent/traefik-ingress-front.yaml` | `/` → `front` |
| `system/proxy_ingress/order-agent/traefik-ingress-back.yaml` | `/api` → `back` |
| `system/proxy_ingress/order-agent/certificate.yaml` | TLS |

namespace는 둘 다 `order-agent-system`이다.

front/back 각각 GitHub Actions가 있으면 경로는 이렇게 둔다.

```yaml
GITOPS_VALUES_PATH: applications/order-agent/front/values.yaml
# 또는
GITOPS_VALUES_PATH: applications/order-agent/back/values.yaml
```

back의 Ingress path 기본값은 `/api`다. 바꾸려면 `--ingress-path`를 쓴다.

### CLI 옵션

| 옵션 | 설명 |
| --- | --- |
| `--name` | 앱 이름. namespace는 `<name>-system` |
| `--component` | 워크로드 이름. 제한 없음. `applications/<name>/<component>/` 에 생성되고 namespace는 `<name>-system` 공유 |
| `--image` | `repository:tag` |
| `--port` | Service 포트 |
| `--target-port` | 컨테이너 포트. 생략하면 `--port`와 같음 |
| `--replicas` | replica 수. 기본 1 |
| `--host` | Ingress 호스트. `system/proxy_ingress/<name>/` 에 YAML 생성 |
| `--ingress-path` | Ingress path. 기본 `/`, `--component back`이면 `/api` |
| `--tls` | cert-manager (`letsencrypt-prod`) + `cert-manager-prod` secret |
| `--pvc` | PVC 크기. 예: `20Gi` |
| `--storage-class` | StorageClass. 비우면 클러스터 기본값 |
| `--mount-path` | 볼륨 마운트 경로. 기본 `/data` |
| `--secret` | SealedSecret 사용 |
| `--secret-file` | `KEY=VALUE` 파일. `kubeseal`로 암호화해 values에 넣음 |
| `--force` | 기존 values.yaml 덮어쓰기 |

`kubeseal`은 컨트롤러 `sealed-secret-sealed-secrets` / namespace `default`를 사용한다. 평문 Secret은 Git에 넣지 않는다.

## Git push

values.yaml을 [localInfra.git](https://github.com/Oldentomato/localInfra.git) `main`에 push한다.

Argo CD가 공통 차트 `charts/service`와 이 values를 합쳐서 Deployment / Service / ServiceAccount를 만든다.

## Ingress 적용

```powershell
kubectl apply -f ../system/proxy_ingress/order-agent/
```

기존 서비스 Ingress와 같은 방식이다. Helm/Argo CD가 이 리소스를 관리하지 않으므로 prune되지 않는다.

## 이후 이미지 배포 (기존 CI/CD와 동일)

CD가 해당 values.yaml의 `image.tag`만 변경한다.

```yaml
image:
  repository: oldentomato/order-api
  tag: "sha-abc1234"
```

키 경로 `image.tag`는 기존 서비스와 같다.

## 하지 말 것

- 기존 앱을 `applications/` 아래로 옮기지 않는다.
- 기존 앱 이름과 같은 디렉터리를 `applications/`에 만들지 않는다.
- Ingress를 Helm values로 켜지 않는다. 이 차트는 Ingress를 렌더하지 않는다.
- `app/minecraft`는 이 흐름과 무관하다. 수정하지 않는다.
- ApplicationSet을 이미 apply 했다면, 이번 YAML 변경 후 **다시 한 번** `kubectl apply -f system/argoCD/services/` 한다.
