{{/*
Common labels for any resource. Workload templates add selector labels for
their component within the release.
*/}}
{{- define "aurum-ai.labels" -}}
app.kubernetes.io/part-of: aurum-ai
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "aurum-ai.selectorLabels" -}}
app.kubernetes.io/name: {{ .component }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
{{- end }}

{{/*
Full image ref for a service: values.image.registry (chart-wide) + the
service's own repository + tag (service tag overrides values.image.tag,
which in turn overrides .Chart.AppVersion).
*/}}
{{- define "aurum-ai.image" -}}
{{- $registry := .root.Values.image.registry -}}
{{- $repository := .service.image.repository -}}
{{- $tag := .service.image.tag | default .root.Values.image.tag | default .root.Chart.AppVersion -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry $repository $tag -}}
{{- else -}}
{{- printf "%s:%s" $repository $tag -}}
{{- end -}}
{{- end }}

{{/* Service override for permanent workload pull policy, then chart default. */}}
{{- define "aurum-ai.imagePullPolicy" -}}
{{- .service.image.pullPolicy | default .root.Values.image.pullPolicy -}}
{{- end }}

{{- define "aurum-ai.secretName" -}}
{{ .Values.secrets.name }}
{{- end }}

{{/* Roll all web consumers when managed or externally managed credentials rotate. */}}
{{- define "aurum-ai.webSecretChecksum" -}}
{{- if .Values.web.existingSecret -}}
{{- printf "external|%s|%s" .Values.web.existingSecret .Values.web.secretRotationToken | sha256sum -}}
{{- else -}}
{{- printf "managed|%s|%s" .Values.web.serviceSecret .Values.web.searxngSecret | sha256sum -}}
{{- end -}}
{{- end }}

{{/* Roll web consumers whenever their rendered managed configuration changes. */}}
{{- define "aurum-ai.webConfigChecksum" -}}
{{- include (print $.Template.BasePath "/web-configmap.yaml") . | sha256sum -}}
{{- end }}

{{/* Resolve the safe kube-dns preset or a validated operator label map. */}}
{{- define "aurum-ai.dnsPodLabels" -}}
{{- if kindIs "string" .Values.networkPolicies.dns.podLabels -}}
{{- if ne .Values.networkPolicies.dns.podLabels "kube-dns" -}}
{{- fail "networkPolicies.dns.podLabels string value must be the kube-dns preset" -}}
{{- end -}}
k8s-app: kube-dns
{{- else -}}
{{- if or (not (kindIs "map" .Values.networkPolicies.dns.podLabels)) (eq (len .Values.networkPolicies.dns.podLabels) 0) -}}
{{- fail "networkPolicies.dns.podLabels must be a non-empty map" -}}
{{- end -}}
{{- range $key, $value := .Values.networkPolicies.dns.podLabels -}}
{{- if or (not ($key | default "")) (not ($value | default "")) -}}
{{- fail "networkPolicies.dns.podLabels keys and values must be non-empty" -}}
{{- end -}}
{{- end -}}
{{- toYaml .Values.networkPolicies.dns.podLabels -}}
{{- end -}}
{{- end }}

{{/* DNS namespace validation applies to unconditional runner policy too. */}}
{{- define "aurum-ai.dnsNamespace" -}}
{{- $namespace := .Values.networkPolicies.dns.namespace | default "" -}}
{{- if or (not $namespace) (not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" $namespace)) (gt (len $namespace) 63) -}}
{{- fail "networkPolicies.dns.namespace must be a non-empty valid DNS label" -}}
{{- end -}}
{{- $namespace -}}
{{- end }}

{{/*
Browser-facing base URL for one of the Ingress hosts. The *browser* has to
reach the Ingress controller through whatever port it is published on —
README.md's k3d flow publishes traefik on 8081 (`-p "8081:80@loadbalancer"`),
so a bare `http://chat.127.0.0.1.nip.io` would hit port 80 with nothing
listening. Set ingress.externalScheme/externalPort to https/443 (or http/80)
for a real cluster and the port drops out of the URL.

Used for both browser URLs (frontend/admin-ui runtimeConfig) and
APP_FRONTEND_URL, which app_factory.py turns into a single exact CORS
allow_origins entry — it must match the Origin the browser actually sends,
port included, or every cross-origin call is rejected.

Args: dict "host" <hostname> "root" $
*/}}
{{- define "aurum-ai.externalUrl" -}}
{{- $scheme := .root.Values.ingress.externalScheme | default "http" -}}
{{- $port := .root.Values.ingress.externalPort | toString -}}
{{- if or (and (eq $scheme "http") (eq $port "80")) (and (eq $scheme "https") (eq $port "443")) (eq $port "") (eq $port "<nil>") -}}
{{- printf "%s://%s" $scheme .host -}}
{{- else -}}
{{- printf "%s://%s:%s" $scheme .host $port -}}
{{- end -}}
{{- end }}

{{/*
The Secret the migration hook Job reads. When the chart creates its own
Secret, the Job gets a *separate*, hook-scoped one (migration-secret.yaml)
that is deleted together with the Job — that keeps the main Secret an
ordinary tracked release resource (uninstalled and rolled back with the
release) instead of an orphan hook resource. When secrets.create is false
the pre-created Secret already exists before the hook runs, so the Job can
read it directly.
*/}}
{{- define "aurum-ai.migrationSecretName" -}}
{{- if .Values.secrets.create -}}
{{ .Release.Name }}-migration-secrets
{{- else -}}
{{ .Values.secrets.name }}
{{- end -}}
{{- end }}

{{/* Namespace for dynamic conversation-runner Pods. */}}
{{- define "aurum-ai.runnerNamespace" -}}
{{- .Values.runtimeNamespaces.runner | default (printf "%s-runners" .Release.Name) | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{/* Namespace for dynamic code-skill sandbox Pods. */}}
{{- define "aurum-ai.sandboxNamespace" -}}
{{- .Values.runtimeNamespaces.sandbox | default (printf "%s-sandboxes" .Release.Name) | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{/* pods/exec isolation requires distinct runner and sandbox namespaces. */}}
{{- define "aurum-ai.validateRuntimeNamespaces" -}}
{{- $runner := include "aurum-ai.runnerNamespace" . -}}
{{- $sandbox := include "aurum-ai.sandboxNamespace" . -}}
{{- if eq $runner .Release.Namespace -}}
{{- fail "runtimeNamespaces.runner must differ from the release namespace" -}}
{{- end -}}
{{- if eq $sandbox .Release.Namespace -}}
{{- fail "runtimeNamespaces.sandbox must differ from the release namespace" -}}
{{- end -}}
{{- if eq $runner $sandbox -}}
{{- fail "runtimeNamespaces.runner and runtimeNamespaces.sandbox must resolve to different namespaces" -}}
{{- end -}}
{{- end }}

{{- define "aurum-ai.runnerSecretName" -}}
{{- .Values.runtimeSecrets.runnerName | default (printf "%s-runner-secrets" .Release.Name) | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "aurum-ai.backendServiceAccountName" -}}
{{- printf "%s-backend" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "aurum-ai.skillExecutorServiceAccountName" -}}
{{- printf "%s-skill-executor" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end }}
