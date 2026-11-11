resource "helm_release" "nvidia_device_plugin" {
  name       = "nvidia-device-plugin"
  namespace  = "kube-system"
  repository = "https://nvidia.github.io/k8s-device-plugin"
  chart      = "nvidia-device-plugin"
  version    = var.nvidia_device_plugin_version
  wait       = true
  timeout    = 300

  # The chart's default affinity relies on NFD labels, which are not installed.
  # Karpenter labels GPU nodes workload=gpu, so schedule on that instead.
  values = [yamlencode({
    affinity = {
      nodeAffinity = {
        requiredDuringSchedulingIgnoredDuringExecution = {
          nodeSelectorTerms = [{
            matchExpressions = [{
              key      = "workload"
              operator = "In"
              values   = ["gpu"]
            }]
          }]
        }
      }
    }
  })]

  depends_on = [helm_release.karpenter_gpu_nodepool]
}
