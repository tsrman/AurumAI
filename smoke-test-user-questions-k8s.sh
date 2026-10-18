#!/usr/bin/env bash
# Manual/exclusive Kubernetes acceptance for durable user questions.
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export K8S_TEST_ACCEPTANCE=user-questions
export QUESTION_ACCEPTANCE_KIND=short
export K8S_TEST_CLUSTER="${K8S_TEST_CLUSTER:-aurum-ai-user-questions}"
export K8S_TEST_RELEASE="${K8S_TEST_RELEASE:-aurum-ai-user-questions}"
export K8S_TEST_NAMESPACE="${K8S_TEST_NAMESPACE:-aurum-ai-user-questions}"
export K8S_TEST_RUNNER_NAMESPACE="${K8S_TEST_RUNNER_NAMESPACE:-aurum-ai-user-questions-runners}"
export K8S_TEST_SANDBOX_NAMESPACE="${K8S_TEST_SANDBOX_NAMESPACE:-aurum-ai-user-questions-sandboxes}"

exec "$ROOT_DIR/smoke-test-k8s.sh" "$@"
