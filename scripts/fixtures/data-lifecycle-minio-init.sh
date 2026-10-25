#!/bin/sh
# Fixed disposable fixture accounts only; never run against a production alias.
set -eu
export MC_HOST_fixture=http://lifecycle-minio:lifecycle-minio-password@minio:9000
attempt=0
until mc ready fixture >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 60 ] || exit 1
  sleep 1
done
for name in business archives; do
  mc mb --ignore-existing "fixture/lifecycle-$name" >/dev/null
  mc admin user add fixture "lifecycle-$name" "lifecycle-$name-password" >/dev/null
  mc admin policy create fixture "lifecycle-$name" "/fixture/data-lifecycle-$name-policy.json" >/dev/null
  mc admin policy attach fixture "lifecycle-$name" --user "lifecycle-$name" >/dev/null
done
# The data worker's conversation image account: the documented runbook policy,
# limited to conversation-images/* in the business bucket.
mc admin user add fixture lifecycle-images lifecycle-images-password >/dev/null
mc admin policy create fixture lifecycle-images /fixture/data-lifecycle-images-policy.json >/dev/null
mc admin policy attach fixture lifecycle-images --user lifecycle-images >/dev/null
