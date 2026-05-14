#!/usr/bin/env bash
# github_client.sh — Write-only GitHub client for the agent.
#
# Enforces:
#   - Repo allowlist (hard reject if repo not listed)
#   - Rate limit for non-operators (default 30 writes/day per user; override via RATE_LIMIT_MAX env)
#   - Attribution footer on non-operator writes
#   - Subcommand whitelist (only issue/pr/allowlist commands)
#
# Token is loaded from file inside this script — Claude CLI never sees it.
# Usage: github_client.sh [--operator] [--user USERNAME] <subcommand> <args...>

set -euo pipefail

# ─── Configuration ──────────────────────────────────────────────────────────
TOKEN_FILE="$HOME/.claude/.github-token"
ALLOWLIST_FILE="$HOME/.claude/.github-repos-allowlist"
# GitHub org/user that newly-created repos belong to (used to auto-add them to
# the allowlist and to compute the canonical full repo name for `repo create`
# and `repo push`). Defaults to empty — set GITHUB_ORG in the env or the
# wrapper will fall back to whatever org `gh repo create` happens to use.
GITHUB_ORG="${GITHUB_ORG:-}"
RATE_LIMIT_FILE="$HOME/.claude/.github-rate-limit"
RATE_LIMIT_MAX="${RATE_LIMIT_MAX:-30}"         # max write actions per non-operator per 24h
RATE_LIMIT_WINDOW="${RATE_LIMIT_WINDOW:-86400}" # 24 hours in seconds

# ─── Load token ─────────────────────────────────────────────────────────────
if [[ ! -f "$TOKEN_FILE" ]]; then
    echo "ERROR: GitHub token not found at $TOKEN_FILE" >&2
    exit 1
fi
export GH_TOKEN
GH_TOKEN="$(cat "$TOKEN_FILE")"

# ─── Parse global flags ─────────────────────────────────────────────────────
IS_OPERATOR=false
CALLER_USER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --operator)
            IS_OPERATOR=true
            shift
            ;;
        --user)
            CALLER_USER="$2"
            shift 2
            ;;
        *)
            break
            ;;
    esac
done

if [[ $# -lt 1 ]]; then
    echo "Usage: github_client.sh [--operator] [--user USERNAME] <subcommand> <args...>" >&2
    echo "Subcommands: issue create|close|comment|edit, pr create|comment, allowlist add|list" >&2
    exit 1
fi

# ─── Subcommand routing ─────────────────────────────────────────────────────
SUBCMD="$1"
shift

case "$SUBCMD" in
    issue)
        [[ $# -lt 1 ]] && { echo "ERROR: issue requires action (create|close|comment|edit)" >&2; exit 1; }
        ACTION="$1"; shift
        ;;
    pr)
        [[ $# -lt 1 ]] && { echo "ERROR: pr requires action (create|comment)" >&2; exit 1; }
        ACTION="$1"; shift
        ;;
    repo)
        [[ $# -lt 1 ]] && { echo "ERROR: repo requires action (create)" >&2; exit 1; }
        ACTION="$1"; shift
        ;;
    allowlist)
        [[ $# -lt 1 ]] && { echo "ERROR: allowlist requires action (add|list)" >&2; exit 1; }
        ACTION="$1"; shift
        ;;
    *)
        echo "ERROR: Unknown subcommand '$SUBCMD'. Allowed: issue, pr, repo, allowlist" >&2
        exit 1
        ;;
esac

# ─── Allowlist helpers ───────────────────────────────────────────────────────

# Check if a repo is in the allowlist. Returns 0 if allowed, 1 if not.
check_allowlist() {
    local repo="$1"
    if [[ ! -f "$ALLOWLIST_FILE" ]]; then
        echo "ERROR: Allowlist file not found at $ALLOWLIST_FILE" >&2
        return 1
    fi
    # Exact line match — no partial matches, no regex
    grep -qxF "$repo" "$ALLOWLIST_FILE"
}

# ─── Rate limit helpers ──────────────────────────────────────────────────────

# Check if non-operator user is within rate limit. Returns 0 if allowed, 1 if at limit.
check_rate_limit() {
    local user="$1"
    local now
    now="$(date +%s)"
    local cutoff=$((now - RATE_LIMIT_WINDOW))

    # Create file if missing
    if [[ ! -f "$RATE_LIMIT_FILE" ]]; then
        echo '{}' > "$RATE_LIMIT_FILE"
    fi

    # Count entries within window for this user — fail closed on parse error
    local count
    count="$(jq -r --arg user "$user" --argjson cutoff "$cutoff" '
        ((.[$user] // []) | map(select(.ts > $cutoff)) | length)
    ' "$RATE_LIMIT_FILE")" || { echo "ERROR: Rate limit file corrupt, rejecting action" >&2; return 1; }
    [[ -z "$count" ]] && { echo "ERROR: Rate limit check returned empty, rejecting action" >&2; return 1; }

    if [[ "$count" -ge "$RATE_LIMIT_MAX" ]]; then
        echo "ERROR: Rate limit exceeded. $user has used $count/$RATE_LIMIT_MAX GitHub actions in the last 24h." >&2
        return 1
    fi
    return 0
}

# Record a write action for rate limiting.
record_action() {
    local user="$1"
    local repo="$2"
    local now
    now="$(date +%s)"
    local cutoff=$((now - RATE_LIMIT_WINDOW))

    if [[ ! -f "$RATE_LIMIT_FILE" ]]; then
        echo '{}' > "$RATE_LIMIT_FILE"
    fi

    # Add entry and prune expired entries for all users
    jq --arg user "$user" --argjson now "$now" --arg repo "$repo" --argjson cutoff "$cutoff" '
        # Prune expired entries for all users
        to_entries | map(.value = [.value[] | select(.ts > $cutoff)]) | from_entries
        # Add new entry for this user
        | .[$user] = ((.[$user] // []) + [{"ts": $now, "repo": $repo}])
    ' "$RATE_LIMIT_FILE" > "$RATE_LIMIT_FILE.tmp" && mv "$RATE_LIMIT_FILE.tmp" "$RATE_LIMIT_FILE"
}

# ─── Attribution ─────────────────────────────────────────────────────────────

# Append attribution footer to body text for non-operator writes.
add_attribution() {
    local body="$1"
    local user="$2"
    printf '%s\n\n---\n*Filed on behalf of @%s in Leviathan Agents Chat*' "$body" "$user"
}

# ─── Command implementations ────────────────────────────────────────────────

cmd_issue_create() {
    # Parse: <repo> --title "..." --body "..."
    local repo="$1"; shift
    local title="" body=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --title) title="$2"; shift 2 ;;
            --body)  body="$2"; shift 2 ;;
            *) echo "ERROR: Unknown flag '$1' for issue create. Allowed: --title, --body" >&2; exit 1 ;;
        esac
    done
    [[ -z "$title" ]] && { echo "ERROR: --title is required" >&2; exit 1; }
    [[ -z "$body" ]]  && { echo "ERROR: --body is required" >&2; exit 1; }

    check_allowlist "$repo" || { echo "ERROR: Repo '$repo' is not in the allowlist." >&2; exit 1; }

    if [[ "$IS_OPERATOR" != "true" ]]; then
        [[ -z "$CALLER_USER" ]] && { echo "ERROR: --user is required for non-operator calls" >&2; exit 1; }
        check_rate_limit "$CALLER_USER" || exit 1
        body="$(add_attribution "$body" "$CALLER_USER")"
    fi

    gh issue create -R "$repo" --title "$title" --body "$body"

    if [[ "$IS_OPERATOR" != "true" ]]; then
        record_action "$CALLER_USER" "$repo"
    fi
}

cmd_issue_close() {
    # Parse: <repo> <number>
    [[ $# -lt 2 ]] && { echo "ERROR: issue close requires <repo> <number>" >&2; exit 1; }
    local repo="$1" number="$2"
    [[ ! "$number" =~ ^[0-9]+$ ]] && { echo "ERROR: issue number must be a positive integer" >&2; exit 1; }
    check_allowlist "$repo" || { echo "ERROR: Repo '$repo' is not in the allowlist." >&2; exit 1; }

    if [[ "$IS_OPERATOR" != "true" ]]; then
        [[ -z "$CALLER_USER" ]] && { echo "ERROR: --user is required for non-operator calls" >&2; exit 1; }
        check_rate_limit "$CALLER_USER" || exit 1
    fi

    gh issue close "$number" -R "$repo"

    if [[ "$IS_OPERATOR" != "true" ]]; then
        record_action "$CALLER_USER" "$repo"
    fi
}

cmd_issue_comment() {
    # Parse: <repo> <number> --body "..."
    [[ $# -lt 2 ]] && { echo "ERROR: issue comment requires <repo> <number>" >&2; exit 1; }
    local repo="$1" number="$2"; shift 2
    [[ ! "$number" =~ ^[0-9]+$ ]] && { echo "ERROR: issue number must be a positive integer" >&2; exit 1; }
    local body=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --body) body="$2"; shift 2 ;;
            *) echo "ERROR: Unknown flag '$1' for issue comment. Allowed: --body" >&2; exit 1 ;;
        esac
    done
    [[ -z "$body" ]] && { echo "ERROR: --body is required" >&2; exit 1; }

    check_allowlist "$repo" || { echo "ERROR: Repo '$repo' is not in the allowlist." >&2; exit 1; }

    if [[ "$IS_OPERATOR" != "true" ]]; then
        [[ -z "$CALLER_USER" ]] && { echo "ERROR: --user is required for non-operator calls" >&2; exit 1; }
        check_rate_limit "$CALLER_USER" || exit 1
        body="$(add_attribution "$body" "$CALLER_USER")"
    fi

    gh issue comment "$number" -R "$repo" --body "$body"

    if [[ "$IS_OPERATOR" != "true" ]]; then
        record_action "$CALLER_USER" "$repo"
    fi
}

cmd_issue_edit() {
    # Parse: <repo> <number> --body "..."
    # Edits the issue body (REPLACES entire body with new content).
    # Operator-only: non-operators cannot overwrite issue bodies (destructive,
    # could wipe operator-authored content even with rate limit + attribution).
    # Non-operators should use `issue comment` instead to add to discussions.
    [[ "$IS_OPERATOR" != "true" ]] && {
        echo "ERROR: 'issue edit' is operator-only — it replaces issue body content." >&2
        echo "Non-operators: use 'issue comment' to add to discussions instead." >&2
        exit 1
    }
    [[ $# -lt 2 ]] && { echo "ERROR: issue edit requires <repo> <number>" >&2; exit 1; }
    local repo="$1" number="$2"; shift 2
    [[ ! "$number" =~ ^[0-9]+$ ]] && { echo "ERROR: issue number must be a positive integer" >&2; exit 1; }
    local body=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --body) body="$2"; shift 2 ;;
            *) echo "ERROR: Unknown flag '$1' for issue edit. Allowed: --body" >&2; exit 1 ;;
        esac
    done
    [[ -z "$body" ]] && { echo "ERROR: --body is required" >&2; exit 1; }

    check_allowlist "$repo" || { echo "ERROR: Repo '$repo' is not in the allowlist." >&2; exit 1; }

    gh issue edit "$number" -R "$repo" --body "$body"
}

cmd_pr_create() {
    # Parse: <repo> --title "..." --body "..." --head "..." --base "..."
    local repo="$1"; shift
    local title="" body="" head="" base=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --title) title="$2"; shift 2 ;;
            --body)  body="$2"; shift 2 ;;
            --head)  head="$2"; shift 2 ;;
            --base)  base="$2"; shift 2 ;;
            *) echo "ERROR: Unknown flag '$1' for pr create. Allowed: --title, --body, --head, --base" >&2; exit 1 ;;
        esac
    done
    [[ -z "$title" ]] && { echo "ERROR: --title is required" >&2; exit 1; }
    [[ -z "$body" ]]  && { echo "ERROR: --body is required" >&2; exit 1; }
    [[ -z "$head" ]]  && { echo "ERROR: --head is required" >&2; exit 1; }
    [[ -z "$base" ]]  && { echo "ERROR: --base is required (e.g. 'main')" >&2; exit 1; }

    check_allowlist "$repo" || { echo "ERROR: Repo '$repo' is not in the allowlist." >&2; exit 1; }

    if [[ "$IS_OPERATOR" != "true" ]]; then
        [[ -z "$CALLER_USER" ]] && { echo "ERROR: --user is required for non-operator calls" >&2; exit 1; }
        check_rate_limit "$CALLER_USER" || exit 1
        body="$(add_attribution "$body" "$CALLER_USER")"
    fi

    gh pr create -R "$repo" --title "$title" --body "$body" --head "$head" --base "$base"

    if [[ "$IS_OPERATOR" != "true" ]]; then
        record_action "$CALLER_USER" "$repo"
    fi
}

cmd_pr_comment() {
    # Parse: <repo> <number> --body "..."
    [[ $# -lt 2 ]] && { echo "ERROR: pr comment requires <repo> <number>" >&2; exit 1; }
    local repo="$1" number="$2"; shift 2
    [[ ! "$number" =~ ^[0-9]+$ ]] && { echo "ERROR: PR number must be a positive integer" >&2; exit 1; }
    local body=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --body) body="$2"; shift 2 ;;
            *) echo "ERROR: Unknown flag '$1' for pr comment. Allowed: --body" >&2; exit 1 ;;
        esac
    done
    [[ -z "$body" ]] && { echo "ERROR: --body is required" >&2; exit 1; }

    check_allowlist "$repo" || { echo "ERROR: Repo '$repo' is not in the allowlist." >&2; exit 1; }

    if [[ "$IS_OPERATOR" != "true" ]]; then
        [[ -z "$CALLER_USER" ]] && { echo "ERROR: --user is required for non-operator calls" >&2; exit 1; }
        check_rate_limit "$CALLER_USER" || exit 1
        body="$(add_attribution "$body" "$CALLER_USER")"
    fi

    gh pr comment "$number" -R "$repo" --body "$body"

    if [[ "$IS_OPERATOR" != "true" ]]; then
        record_action "$CALLER_USER" "$repo"
    fi
}

cmd_repo_create() {
    # Parse: <name> --description "..."
    [[ $# -lt 1 ]] && { echo "ERROR: repo create requires <name>" >&2; exit 1; }
    local name="$1"; shift
    local description=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --description) description="$2"; shift 2 ;;
            *) echo "ERROR: Unknown flag '$1' for repo create. Allowed: --description" >&2; exit 1 ;;
        esac
    done
    # Validate name: alphanumeric, hyphens, underscores, dots
    if [[ ! "$name" =~ ^[a-zA-Z0-9._-]+$ ]]; then
        echo "ERROR: Invalid repo name. Use alphanumeric, hyphens, underscores, dots." >&2
        exit 1
    fi
    # Create public repo under the authenticated user's account
    local args=("--public")
    [[ -n "$description" ]] && args+=("--description" "$description")
    gh repo create "$name" "${args[@]}"
    # Auto-add to allowlist so the agent can interact with the new repo
    local full_name="${GITHUB_ORG:-$(gh api user --jq .login)}/$name"
    if [[ -f "$ALLOWLIST_FILE" ]] && grep -qxF "$full_name" "$ALLOWLIST_FILE" 2>/dev/null; then
        : # already in allowlist
    else
        echo "$full_name" >> "$ALLOWLIST_FILE"
        echo "Auto-added '$full_name' to the allowlist."
    fi
}

cmd_repo_push() {
    # Usage: repo push <name> [workdir] [--description "..."]
    # Creates the repo (if missing), sets origin, pushes the workdir's `main` branch,
    # auto-allowlists, and prints the canonical https URL on success.
    # The workdir must already be a git repo with at least one commit on `main`.
    [[ $# -lt 1 ]] && { echo "ERROR: repo push requires <name> [workdir]" >&2; exit 1; }
    local name="$1"; shift
    local workdir="."
    local description=""
    if [[ $# -gt 0 && "$1" != --* ]]; then
        workdir="$1"; shift
    fi
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --description) description="$2"; shift 2 ;;
            *) echo "ERROR: Unknown flag '$1' for repo push." >&2; exit 1 ;;
        esac
    done
    if [[ ! "$name" =~ ^[a-zA-Z0-9._-]+$ ]]; then
        echo "ERROR: Invalid repo name. Use alphanumeric, hyphens, underscores, dots." >&2
        exit 1
    fi
    if [[ ! -d "$workdir/.git" ]]; then
        echo "ERROR: '$workdir' is not a git repo. Run 'git init && git add . && git commit' first." >&2
        exit 1
    fi
    cd "$workdir"
    # Ensure branch is named main (gh push expects HEAD branch matches remote default)
    local current_branch
    current_branch="$(git symbolic-ref --short HEAD 2>/dev/null || echo "")"
    if [[ "$current_branch" != "main" ]]; then
        git branch -M main
    fi
    # Create repo if it doesn't exist; ignore conflict if it does
    local args=("--public")
    [[ -n "$description" ]] && args+=("--description" "$description")
    if ! gh repo view "$name" >/dev/null 2>&1; then
        gh repo create "$name" "${args[@]}" --source=. --remote=origin --push
    else
        # Repo exists — make sure origin points at it and push
        local existing_url
        existing_url="$(gh repo view "$name" --json url -q .url)"
        if ! git remote get-url origin >/dev/null 2>&1; then
            git remote add origin "$existing_url"
        fi
        # gh credential helper uses GH_TOKEN automatically for https pushes
        gh auth setup-git >/dev/null 2>&1 || true
        git push -u origin main
    fi
    # Auto-allowlist
    local full_name="${GITHUB_ORG:-$(gh api user --jq .login)}/$name"
    if [[ -f "$ALLOWLIST_FILE" ]] && grep -qxF "$full_name" "$ALLOWLIST_FILE" 2>/dev/null; then
        : # already
    else
        echo "$full_name" >> "$ALLOWLIST_FILE"
    fi
    # Print final URL on its own line so callers can grep it
    gh repo view "$name" --json url -q .url
}

cmd_allowlist_add() {
    local repo="$1"
    # Validate format: owner/repo
    if [[ ! "$repo" =~ ^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$ ]]; then
        echo "ERROR: Invalid repo format. Expected 'owner/repo'." >&2
        exit 1
    fi
    # Check if already in allowlist
    if grep -qxF "$repo" "$ALLOWLIST_FILE" 2>/dev/null; then
        echo "Repo '$repo' is already in the allowlist."
        exit 0
    fi
    echo "$repo" >> "$ALLOWLIST_FILE"
    echo "Added '$repo' to the allowlist."
}

cmd_allowlist_list() {
    if [[ ! -f "$ALLOWLIST_FILE" ]]; then
        echo "(empty — no allowlist file found)"
        exit 0
    fi
    echo "Allowed repos:"
    cat "$ALLOWLIST_FILE"
}

# ─── Dispatch ────────────────────────────────────────────────────────────────

case "$SUBCMD/$ACTION" in
    issue/create)   cmd_issue_create "$@" ;;
    issue/close)    cmd_issue_close "$@" ;;
    issue/comment)  cmd_issue_comment "$@" ;;
    issue/edit)     cmd_issue_edit "$@" ;;
    pr/create)      cmd_pr_create "$@" ;;
    pr/comment)     cmd_pr_comment "$@" ;;
    repo/create)    cmd_repo_create "$@" ;;
    repo/push)      cmd_repo_push "$@" ;;
    allowlist/add)  cmd_allowlist_add "$@" ;;
    allowlist/list) cmd_allowlist_list ;;
    *)
        echo "ERROR: Unknown command '$SUBCMD $ACTION'." >&2
        echo "Allowed: issue create|close|comment|edit, pr create|comment, repo create|push, allowlist add|list" >&2
        exit 1
        ;;
esac
