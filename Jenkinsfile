// Reference RAG stage: integrate after service build/image stages in the deployment pipeline.
pipeline {
    agent { label 'python312-docker-kubectl-pwsh' }
    parameters {
        string(name: 'NAMESPACE', defaultValue: 'incident-lab')
        string(name: 'INDEXER_REPOSITORY', defaultValue: 'incident-lab-indexer')
        booleanParam(name: 'PUSH_IMAGE', defaultValue: false)
        booleanParam(name: 'PUBLISH_DEPLOYED_COMMIT', defaultValue: false,
            description: 'Enable only after these exact service images have been deployed successfully.')
    }
    stages {
        stage('Checkout') {
            steps { checkout scm }
        }
        stage('Offline RAG checks') {
            steps {
                sh '''
                    set -eu
                    python3.12 -m venv .venv
                    .venv/bin/python -m pip install -r indexer/requirements.txt
                    .venv/bin/python -m unittest discover -s tests -p test_rag.py -v
                    mkdir -p tmp
                    .venv/bin/python -m tests.rag_compare > tmp/rag-comparison.json
                '''
            }
        }
        stage('Build commit-pinned index') {
            steps {
                sh '''
                    set -eu
                    .venv/bin/python - <<'PY'
import os
from pathlib import Path
import yaml
document = yaml.safe_load(Path("config/services.yaml").read_text())
for service in document["services"]:
    service["prod_commit"] = os.environ["GIT_COMMIT"]
    service["commit_kind"] = "git"
Path("tmp/services.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
PY
                    set --
                    if [ "$PUSH_IMAGE" = true ]; then set -- -PushImage; fi
                    pwsh -NoProfile -File scripts/index-code.ps1 \
                        -Namespace "$NAMESPACE" -ConfigPath tmp/services.yaml \
                        -PythonExecutable .venv/bin/python \
                        -Image "$INDEXER_REPOSITORY:$GIT_COMMIT" \
                        -JobName "code-indexer-$(printf %.12s "$GIT_COMMIT")" \
                        "$@"
                '''
            }
        }
        stage('Publish deployed commit metadata') {
            when { expression { params.PUBLISH_DEPLOYED_COMMIT } }
            steps {
                sh '''
                    set -eu
                    kubectl create configmap services-config -n "$NAMESPACE" \
                        --from-file=services.yaml=tmp/services.yaml --dry-run=client -o json | kubectl apply -f -
                '''
            }
        }
    }
    post { always { archiveArtifacts artifacts: 'tmp/rag-comparison.json', allowEmptyArchive: true } }
}