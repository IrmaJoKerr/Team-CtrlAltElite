resource "null_resource" "rag_corpus" {

  provisioner "local-exec" {
    command = <<EOT
set -e

echo "Creating Vertex AI RAG corpus (if not exists)..."

gcloud alpha ai rag corpora create \
  --display-name="${var.display_name}" \
  --description="${var.description}" \
  --project="${var.project_id}" \
  --region="${var.region}" || echo "RAG corpus already exists"
EOT
  }
}
