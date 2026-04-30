#!/usr/bin/env bash
#
# Deploy MCP Web Search Server to Azure Kubernetes Service (AKS).
#
# Usage:
#   ./deploy-aks.sh                                          # Full deployment
#   ./deploy-aks.sh --skip-infra                             # Only rebuild & redeploy
#   ./deploy-aks.sh --resource-group my-rg --location westus2
#
# Environment overrides (or pass as flags):
#   RESOURCE_GROUP, LOCATION, CLUSTER_NAME, ACR_NAME,
#   NAMESPACE, SEARXNG_SECRET, NODE_COUNT, NODE_VM_SIZE

set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────────────────────────────────────

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-mcp-websearch}"
LOCATION="${LOCATION:-eastasia}"
CLUSTER_NAME="${CLUSTER_NAME:-aks-mcp-websearch}"
ACR_NAME="${ACR_NAME:-acrmcpwebsearch}"
NAMESPACE="${NAMESPACE:-mcp-websearch}"
SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-}"
SEARXNG_SECRET="${SEARXNG_SECRET:-}"
NODE_COUNT="${NODE_COUNT:-1}"
NODE_VM_SIZE="${NODE_VM_SIZE:-Standard_B2s}"
SKIP_INFRA=false

# ─────────────────────────────────────────────────────────────────────────────
# Parse arguments
# ─────────────────────────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --resource-group)  RESOURCE_GROUP="$2"; shift 2 ;;
        --location)        LOCATION="$2"; shift 2 ;;
        --cluster-name)    CLUSTER_NAME="$2"; shift 2 ;;
        --acr-name)        ACR_NAME="$2"; shift 2 ;;
        --subscription)    SUBSCRIPTION_ID="$2"; shift 2 ;;
        --namespace)       NAMESPACE="$2"; shift 2 ;;
        --secret)          SEARXNG_SECRET="$2"; shift 2 ;;
        --node-count)      NODE_COUNT="$2"; shift 2 ;;
        --node-vm-size)    NODE_VM_SIZE="$2"; shift 2 ;;
        --skip-infra)      SKIP_INFRA=true; shift ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | grep '^#' | sed 's/^# \?//'
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─────────────────────────────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────────────────────────────

step()  { printf '\n\033[36m>> %s\033[0m\n' "$1"; }
ok()    { printf '   \033[32m[OK]\033[0m %s\n' "$1"; }
warn()  { printf '   \033[33m[WARN]\033[0m %s\n' "$1"; }
fail()  { printf '   \033[31m[FAIL]\033[0m %s\n' "$1"; }

# ─────────────────────────────────────────────────────────────────────────────
# Preflight checks — auto-install missing tools
# ─────────────────────────────────────────────────────────────────────────────

step "Preflight checks"

declare -A INSTALL_CMDS=(
    [az]="curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash"
    [kubectl]="sudo az aks install-cli"
    [docker]="sudo apt-get update && sudo apt-get install -y docker.io && sudo usermod -aG docker \$USER"
)

MISSING=()
for cmd in az kubectl docker; do
    if command -v "$cmd" &>/dev/null; then
        ok "$cmd found"
    else
        fail "$cmd not found"
        MISSING+=("$cmd")
    fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo ""
    echo "   Missing commands detected. Installing..."
    echo ""
    for cmd in "${MISSING[@]}"; do
        step "Installing $cmd"
        eval "${INSTALL_CMDS[$cmd]}"
        if command -v "$cmd" &>/dev/null; then
            ok "$cmd installed"
        else
            fail "Failed to install $cmd. Please install manually:"
            echo "     ${INSTALL_CMDS[$cmd]}"
            exit 1
        fi
    done
fi

# Azure login check
if [[ -z "$SUBSCRIPTION_ID" ]]; then
    echo ""
    read -rp "   Enter Azure Subscription ID: " SUBSCRIPTION_ID
    if [[ -z "$SUBSCRIPTION_ID" ]]; then
        fail "Subscription ID is required."
        exit 1
    fi
fi

if ! az account show &>/dev/null; then
    echo ""
    fail "Not logged in to Azure. Running 'az login'..."
    az login
fi
ok "Azure CLI authenticated"

az account set --subscription "$SUBSCRIPTION_ID"
ok "Subscription set to: $SUBSCRIPTION_ID"

# Generate SearXNG secret if not provided
if [[ -z "$SEARXNG_SECRET" ]]; then
    SEARXNG_SECRET=$(openssl rand -hex 32)
    ok "Generated SearXNG secret"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Infrastructure provisioning
# ─────────────────────────────────────────────────────────────────────────────

if [[ "$SKIP_INFRA" == false ]]; then

    # Resource Group
    step "Creating Resource Group: $RESOURCE_GROUP"
    EXISTING_LOCATION=$(az group show --name "$RESOURCE_GROUP" --query location -o tsv 2>/dev/null || true)
    if [[ -n "$EXISTING_LOCATION" ]]; then
        LOCATION="$EXISTING_LOCATION"
        ok "Resource Group already exists in $LOCATION, using that location"
    else
        az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none
        ok "Resource Group created in $LOCATION"
    fi

    # ACR
    step "Creating Azure Container Registry: $ACR_NAME"
    if az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" &>/dev/null; then
        ok "ACR already exists"
    else
        az acr create --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --sku Basic --output none
        ok "ACR created"
    fi

    # AKS Cluster
    step "Creating AKS cluster: $CLUSTER_NAME (this may take several minutes)"
    if az aks show --name "$CLUSTER_NAME" --resource-group "$RESOURCE_GROUP" &>/dev/null; then
        ok "AKS cluster already exists"
        az aks update --name "$CLUSTER_NAME" --resource-group "$RESOURCE_GROUP" \
            --attach-acr "$ACR_NAME" --output none 2>/dev/null || true
    else
        az aks create \
            --name "$CLUSTER_NAME" \
            --resource-group "$RESOURCE_GROUP" \
            --location "$LOCATION" \
            --node-count "$NODE_COUNT" \
            --node-vm-size "$NODE_VM_SIZE" \
            --attach-acr "$ACR_NAME" \
            --generate-ssh-keys \
            --output none
        ok "AKS cluster created"
    fi

    # Get credentials
    step "Fetching AKS credentials"
    az aks get-credentials --name "$CLUSTER_NAME" --resource-group "$RESOURCE_GROUP" --overwrite-existing
    ok "kubectl configured"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Build and push MCP image
# ─────────────────────────────────────────────────────────────────────────────

step "Building and pushing MCP image to ACR"
ACR_LOGIN_SERVER=$(az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --query loginServer -o tsv | tr -d '[:space:]')
MCP_IMAGE="${ACR_LOGIN_SERVER}/mcp-web-search:latest"

az acr login --name "$ACR_NAME"
docker build -t "$MCP_IMAGE" "$SCRIPT_DIR/mcp"
docker push "$MCP_IMAGE"
ok "Image pushed: $MCP_IMAGE"

# ─────────────────────────────────────────────────────────────────────────────
# Generate and apply Kubernetes manifests
# ─────────────────────────────────────────────────────────────────────────────

step "Deploying to Kubernetes namespace: $NAMESPACE"

SEARXNG_SETTINGS_CONTENT=$(sed 's/^/    /' "$SCRIPT_DIR/searxng/settings.yml")

MANIFEST_PATH="/tmp/mcp-aks-manifest.yaml"

cat > "$MANIFEST_PATH" <<EOF
---
apiVersion: v1
kind: Namespace
metadata:
  name: $NAMESPACE
---
apiVersion: v1
kind: Secret
metadata:
  name: searxng-secret
  namespace: $NAMESPACE
type: Opaque
stringData:
  SEARXNG_SECRET: "$SEARXNG_SECRET"
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: searxng-settings
  namespace: $NAMESPACE
data:
  settings.yml: |
$SEARXNG_SETTINGS_CONTENT
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: mcp-config
  namespace: $NAMESPACE
data:
  MCP_TRANSPORT: "sse"
  MCP_HOST: "0.0.0.0"
  MCP_PORT: "3000"
  SEARXNG_URL: "http://searxng-svc:8080"
  SEARXNG_TIMEOUT: "25"
  PAGE_TIMEOUT: "15000"
  FETCH_CONCURRENCY: "5"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: searxng
  namespace: $NAMESPACE
  labels:
    app: searxng
spec:
  replicas: 1
  selector:
    matchLabels:
      app: searxng
  template:
    metadata:
      labels:
        app: searxng
    spec:
      containers:
        - name: searxng
          image: searxng/searxng:latest
          ports:
            - containerPort: 8080
          envFrom:
            - secretRef:
                name: searxng-secret
          volumeMounts:
            - name: settings
              mountPath: /etc/searxng/settings.yml
              subPath: settings.yml
              readOnly: true
          readinessProbe:
            httpGet:
              path: /
              port: 8080
            initialDelaySeconds: 15
            periodSeconds: 10
            timeoutSeconds: 5
          livenessProbe:
            httpGet:
              path: /
              port: 8080
            initialDelaySeconds: 30
            periodSeconds: 15
            timeoutSeconds: 5
          resources:
            requests:
              cpu: "100m"
              memory: "256Mi"
            limits:
              cpu: "500m"
              memory: "512Mi"
      volumes:
        - name: settings
          configMap:
            name: searxng-settings
---
apiVersion: v1
kind: Service
metadata:
  name: searxng-svc
  namespace: $NAMESPACE
spec:
  selector:
    app: searxng
  ports:
    - port: 8080
      targetPort: 8080
  type: ClusterIP
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mcp
  namespace: $NAMESPACE
  labels:
    app: mcp
spec:
  replicas: 1
  selector:
    matchLabels:
      app: mcp
  template:
    metadata:
      labels:
        app: mcp
    spec:
      containers:
        - name: mcp
          image: $MCP_IMAGE
          ports:
            - containerPort: 3000
          envFrom:
            - configMapRef:
                name: mcp-config
          readinessProbe:
            httpGet:
              path: /sse
              port: 3000
            initialDelaySeconds: 30
            periodSeconds: 10
            timeoutSeconds: 5
          livenessProbe:
            httpGet:
              path: /sse
              port: 3000
            initialDelaySeconds: 45
            periodSeconds: 15
            timeoutSeconds: 5
          resources:
            requests:
              cpu: "200m"
              memory: "512Mi"
            limits:
              cpu: "1000m"
              memory: "1Gi"
          volumeMounts:
            - name: dshm
              mountPath: /dev/shm
      volumes:
        - name: dshm
          emptyDir:
            medium: Memory
            sizeLimit: "512Mi"
---
apiVersion: v1
kind: Service
metadata:
  name: mcp-svc
  namespace: $NAMESPACE
spec:
  selector:
    app: mcp
  ports:
    - port: 3000
      targetPort: 3000
  type: LoadBalancer
EOF

kubectl apply -f "$MANIFEST_PATH"
ok "Kubernetes resources applied"

# ─────────────────────────────────────────────────────────────────────────────
# Wait for rollout
# ─────────────────────────────────────────────────────────────────────────────

step "Waiting for deployments to be ready..."
kubectl rollout status deployment/searxng -n "$NAMESPACE" --timeout=120s
kubectl rollout status deployment/mcp -n "$NAMESPACE" --timeout=180s
ok "All deployments ready"

# ─────────────────────────────────────────────────────────────────────────────
# Output connection info
# ─────────────────────────────────────────────────────────────────────────────

step "Deployment complete!"

EXTERNAL_IP=""
for i in $(seq 1 30); do
    EXTERNAL_IP=$(kubectl get svc mcp-svc -n "$NAMESPACE" -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
    if [[ -n "$EXTERNAL_IP" ]]; then break; fi
    sleep 10
done

if [[ -z "$EXTERNAL_IP" ]]; then
    warn "External IP not yet assigned. Check later with:"
    echo "   kubectl get svc mcp-svc -n $NAMESPACE"
else
    echo ""
    printf '   \033[32mMCP SSE Endpoint: http://%s:3000/sse\033[0m\n' "$EXTERNAL_IP"
    echo ""
    echo "   Connect your MCP client (Claude, Cursor, LM Studio, etc.) to:"
    printf '   \033[33mhttp://%s:3000/sse\033[0m\n' "$EXTERNAL_IP"
fi

echo ""
echo "Useful commands:"
echo "   kubectl get pods -n $NAMESPACE"
echo "   kubectl logs -f deployment/mcp -n $NAMESPACE"
echo "   kubectl get svc -n $NAMESPACE"
echo ""
