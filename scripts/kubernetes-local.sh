#!/usr/bin/env bash
# Sourced by kubernetes-deploy.sh. Keep all operator files outside Git's index.
: "${root:?Set root before sourcing kubernetes-local.sh}"

# Resolve dot segments and reject symlinks at any component before reading/writing.
# No realpath/Python dependency, including for paths whose parents do not exist.
local_path() {
  local input="$1" part resolved=""
  local -a components
  [[ -n "$input" && "$input" != *$'\n'* && "$input" != *$'\r'* ]] || die 'Expected a non-empty single-line file path'
  [[ "$input" = /* ]] || input="$root/$input"
  IFS=/ read -r -a components <<<"$input"
  for part in "${components[@]}"; do
    case "$part" in
      ''|.) continue ;;
      ..) resolved="${resolved%/*}" ;;
      *) resolved="$resolved/$part"
         [ ! -L "$resolved" ] || die "Symlink paths are not supported: $1" ;;
    esac
  done
  printf '%s\n' "${resolved:-/}"
}

init_local_git() {
  command -v git >/dev/null || die 'Git is required to protect local Kubernetes files'
  local owner
  if owner="$(git -C "$root" rev-parse --show-toplevel 2>/dev/null)"; then
    [ "$owner" = "$root" ] || die 'Run the helper from its own repository/source root'
    local_git=(git -C "$root")
  else
    [ ! -e "$root/.git" ] && [ ! -L "$root/.git" ] || die 'Git metadata is unavailable; preserve this checkout and recover it first'
    # A source archive has no index. Evaluate its ignore rules using a disposable
    # Git directory, without initializing or modifying the transferred sources.
    guard_dir="$(mktemp -d)"
    git init --quiet "$guard_dir"
    local_git=(git --git-dir "$guard_dir/.git" --work-tree "$root")
  fi
}

assert_git_ignored() {
  local input="$1" relative="$2" kind="$3" tracked
  shift 3
  tracked="$("$@" --literal-pathspecs ls-files -- "$relative")" || \
    die 'Cannot inspect the Git index; preserve this checkout and recover it before creating local files'
  [ -z "$tracked" ] || die "Local path is already tracked by Git: $input; preserve the file and remove it from the index separately"
  [ "$kind" != directory ] || relative="$relative/"
  "$@" check-ignore --no-index --quiet -- "$relative" || \
    die "Local path is not ignored by Git: $input; choose .local/kubernetes/ or add an explicit ignore rule first"
}

assert_local_path() {
  local input="$1" kind="${2:-file}" absolute relative parent owner
  absolute="$(local_path "$input")"
  if [ "$kind" = file ] && [ -e "$absolute" ]; then
    [ -f "$absolute" ] || die "Expected a regular file: $input"
  fi
  case "$absolute" in
    "$root"/*)
      relative="${absolute#"$root"/}"
      assert_git_ignored "$input" "$relative" "$kind" "${local_git[@]}" ;;
  esac
  parent="$(dirname "$absolute")"
  if [ "$kind" = directory ] && [ -d "$absolute" ]; then parent="$absolute"; fi
  while [ ! -d "$parent" ]; do parent="$(dirname "$parent")"; done
  if owner="$(git -C "$parent" rev-parse --show-toplevel 2>/dev/null)"; then
    if [ "$owner" != "$root" ]; then
      # Nested checkouts and external checkouts each own their separate index.
      relative="${absolute#"$owner"/}"
      if [ "$absolute" = "$owner" ]; then relative=.; fi
      assert_git_ignored "$input" "$relative" "$kind" git -C "$owner"
    fi
  else
    while true; do
      [ ! -e "$parent/.git" ] && [ ! -L "$parent/.git" ] || \
        die 'Git metadata is unavailable; preserve this checkout and recover it before creating local files'
      [ "$parent" != / ] || break
      parent="$(dirname "$parent")"
    done
  fi
}
