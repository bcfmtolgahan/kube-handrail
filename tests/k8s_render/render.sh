#!/usr/bin/env bash
# Render the namespace policy and the cluster manifests through
# terraform/modules/namespace-policy, without AWS credentials.
#
#   tests/k8s_render/render.sh <fixture.tfvars> <out-dir>   render one fixture into out-dir
#   tests/k8s_render/render.sh --check                       diff every fixture against golden/
#   tests/k8s_render/render.sh --update                      rewrite golden/ from the fixtures
#
# Needs terraform (1.5 or later) and python3. Terraform's working data and state go to a
# temporary directory, never into the repository.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
export TF_DATA_DIR="$work/tf"
export TF_IN_AUTOMATION=1

terraform -chdir="$here" init -input=false -reconfigure -backend-config="path=$work/terraform.tfstate" >/dev/null

render() {
  local vars=$1 out=$2
  terraform -chdir="$here" apply -auto-approve -input=false -var-file="$vars" >/dev/null
  rm -rf "$out"
  mkdir -p "$out"
  terraform -chdir="$here" output -json files |
    python3 -c 'import json, pathlib, sys
out = pathlib.Path(sys.argv[1])
for name, text in json.load(sys.stdin).items():
    (out / name).write_text(text)' "$out"
}

case "${1:-}" in
  --check | --update)
    status=0
    for vars in "$here"/fixtures/*.tfvars; do
      name="$(basename "$vars" .tfvars)"
      render "$vars" "$work/out/$name"
      if [[ "$1" == --update ]]; then
        rm -rf "$here/golden/$name"
        mkdir -p "$here/golden"
        cp -R "$work/out/$name" "$here/golden/$name"
        echo "updated golden/$name"
      elif diff -ru "$here/golden/$name" "$work/out/$name"; then
        echo "PASS  $name"
      else
        echo "FAIL  $name: the rendered files differ from golden/$name (render.sh --update after review)"
        status=1
      fi
    done
    exit "$status"
    ;;
  "" | -*)
    echo "usage: $0 <fixture.tfvars> <out-dir> | --check | --update" >&2
    exit 2
    ;;
  *)
    [[ $# -eq 2 ]] || { echo "usage: $0 <fixture.tfvars> <out-dir>" >&2; exit 2; }
    vars="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
    render "$vars" "$2"
    ;;
esac
