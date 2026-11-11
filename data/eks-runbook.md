# EKS Service Troubleshooting Runbook

When a Kubernetes service becomes unhealthy, first determine whether the problem affects the service, pods, nodes, or an external dependency.

Check deployment status, pod readiness, restart counts, Kubernetes events, and service endpoints.

If pods are healthy but requests are slow, investigate downstream dependencies before increasing replica count.

Check CPU and memory saturation before assuming that additional pods will solve the problem.

For application connectivity problems, verify service endpoints, security groups, DNS resolution, and downstream service health.

Do not restart workloads as the first troubleshooting action unless there is evidence that restarting will address the failure.