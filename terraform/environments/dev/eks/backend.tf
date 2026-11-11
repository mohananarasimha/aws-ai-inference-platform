# Local state for now. Move to a remote backend (e.g. S3) before sharing or automating.
terraform {
  backend "local" {
    path = "terraform.tfstate"
  }
}
