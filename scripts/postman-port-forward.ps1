param([string]$Service = 'order-service', [int]$LocalPort = 18080)
$ErrorActionPreference = 'Stop'
kubectl port-forward -n incident-lab "svc/$Service" "$LocalPort`:8080"
