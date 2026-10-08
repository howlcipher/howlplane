#!/usr/bin/env bash
# Independent behavioural checks for run-044 (relnotes --repo). Run from the target repo after `npm run build`.
set -u
TGT=$PWD; CLI="node $TGT/dist/src/main.js"
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
pass=0; fail=0
check() { if eval "$2"; then echo "PASS $1"; pass=$((pass+1)); else echo "FAIL $1"; fail=$((fail+1)); fi; }

R="$W/repo"; mkdir "$R"; cd "$R"
export GIT_CONFIG_NOSYSTEM=1 HOME="$W" GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@e GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@e
n=0; c() { n=$((n+1)); export GIT_AUTHOR_DATE="2026-01-01T00:00:$(printf %02d $n)Z" GIT_COMMITTER_DATE="2026-01-01T00:00:$(printf %02d $n)Z"; echo $n >> f; git add f; git commit -q -m "$@"; }
git init -q -b main
c "chore: initial"; git tag v1.0.0
c "feat(cli): add export"
c "fix: handle | pipes	and tabs \"quoted\" 'single' ünïcødé ✓"
git switch -q -c topic; git commit -q --allow-empty -m "perf: faster parse"; git switch -q main
c "Update readme"
export GIT_AUTHOR_DATE="2026-01-01T00:01:00Z" GIT_COMMITTER_DATE="2026-01-01T00:01:00Z"
git merge -q --no-ff topic -m "Merge branch 'topic'" || { echo "FIXTURE MERGE FAILED"; exit 9; }
c "feat!: drop node 18"
c "refactor: rename internals" -m "Body line one." -m "BREAKING CHANGE: config moved"
c "fix: mention BREAKING CHANGE: in subject only" -m "  BREAKING CHANGE: indented, does not count"
git tag v1.1.0
cd - >/dev/null

out=$($CLI --repo "$R" --from v1.0.0 --to v1.1.0 --version 1.1.0 2>"$W/err"); code=$?
echo "----- output"; echo "$out"; echo "-----"
check "range exit 0" "[ $code = 0 ]"
check "Breaking changes is first section" "echo \"\$out\" | grep -m1 '^### ' | grep -qx '### Breaking changes'"
check "feat! listed under Breaking" "echo \"\$out\" | awk '/^### Breaking/{f=1;next}/^### /{f=0}f' | grep -q 'drop node 18'"
check "BREAKING CHANGE footer listed under Breaking" "echo \"\$out\" | awk '/^### Breaking/{f=1;next}/^### /{f=0}f' | grep -q 'rename internals'"
check "breaking commits not duplicated" "[ \$(echo \"\$out\" | grep -c 'drop node 18') = 1 ] && [ \$(echo \"\$out\" | grep -c 'rename internals') = 1 ]"
check "indented BREAKING CHANGE line does not count" "echo \"\$out\" | awk '/^### Bug fixes/{f=1;next}/^### /{f=0}f' | grep -q 'mention BREAKING'"
check "merge commit skipped" "! echo \"\$out\" | grep -q 'Merge branch'"
check "merged-branch commit included" "echo \"\$out\" | grep -q 'faster parse'"
check "awkward subject intact" "echo \"\$out\" | grep -qF \"handle | pipes	and tabs \\\"quoted\\\" 'single' ünïcødé ✓\""
check "scope bold + hash format" "echo \"\$out\" | grep -qE '^- \*\*cli:\*\* add export \([0-9a-f]{7}\)$'"
check "hash matches commit" "echo \"\$out\" | grep -q \"add export (\$(git -C '$R' log --format=%h --abbrev=7 -1 --grep='add export'))\""
check "base commit excluded" "! echo \"\$out\" | grep -q initial"
check "oldest first in Bug fixes" "[ \"\$(echo \"\$out\" | awk '/^### Bug fixes/{f=1;next}/^### /{f=0}f' | grep -n . | head -1 | grep -c pipes)\" = 1 ]"
check "header" "echo \"\$out\" | head -1 | grep -qx '## 1.1.0'"
check "no stderr" "[ ! -s '$W/err' ]"
d=$($CLI --repo "$R" --from v1.0.0 --version x 2>/dev/null)
check "default --to HEAD equals explicit" "[ \"\$d\" = \"\$($CLI --repo '$R' --from v1.0.0 --to HEAD --version x)\" ]"
e=$($CLI --repo "$R" --from v1.1.0 --version 2.0.0 2>&1); code=$?
check "empty range -> No changes., exit 0" "[ $code = 0 ] && [ \"\$e\" = \$'## 2.0.0\n\nNo changes.' ]"
e=$($CLI --repo "$R" --from v9.9.9 --version x 2>&1 >/dev/null); code=$?
check "unknown --from exit 2 names ref, one line" "[ $code = 2 ] && echo \"\$e\" | grep -q v9.9.9 && [ \$(echo \"\$e\" | wc -l) = 1 ]"
e=$($CLI --repo "$R" --from v1.0.0 --to nope --version x 2>&1 >/dev/null); code=$?
check "unknown --to exit 2 names ref, one line" "[ $code = 2 ] && echo \"\$e\" | grep -q nope && [ \$(echo \"\$e\" | wc -l) = 1 ]"
mkdir "$W/plain"; e=$($CLI --repo "$W/plain" --from v1 --version x 2>&1 >/dev/null); code=$?
check "not a repo exit 2, one line" "[ $code = 2 ] && [ \$(echo \"\$e\" | wc -l) = 1 ]"
e=$($CLI --repo "$W/missing" --from v1 --version x 2>&1 >/dev/null); code=$?
check "missing path exit 2, one line" "[ $code = 2 ] && [ \$(echo \"\$e\" | wc -l) = 1 ]"
e=$(env PATH=/nonexistent "$(command -v node)" "$TGT/dist/src/main.js" --repo "$R" --from v1.0.0 --version x 2>&1 >/dev/null); code=$?
check "git not installed exit 2, one line" "[ $code = 2 ] && [ \$(echo \"\$e\" | wc -l) = 1 ] && echo \"\$e\" | grep -qi git"
e=$($CLI --repo "$R" --from 'v1.0.0;touch '"$W"'/pwned' --version x 2>&1); code=$?
check "ref with shell metachar: exit 2, no shell" "[ $code = 2 ] && [ ! -e '$W/pwned' ]"
e=$($CLI --repo "$R" --from=--output="$W/clobber" --version x 2>&1); code=$?
check "option-looking ref rejected, no file written" "[ $code = 2 ] && [ -z \"\$(ls '$W' | grep clobber)\" ]"
check "--repo without --from is usage error" "$CLI --repo '$R' --version x >/dev/null 2>&1; [ \$? = 2 ]"
printf 'feat: a\n' > "$W/c.txt"
check "--input with --repo is usage error" "$CLI --input '$W/c.txt' --repo '$R' --from v1.0.0 --version x >/dev/null 2>&1; [ \$? = 2 ]"
check "neither input is usage error" "$CLI --version x >/dev/null 2>&1; [ \$? = 2 ]"
printf 'feat: add export\nfix!: drop flag\nfix: handle tabs\n' > "$W/c.txt"
f=$($CLI --input "$W/c.txt" --version 2.0.0)
check "file input: ! rule, no hashes" "[ \"\$f\" = \$'## 2.0.0\n\n### Breaking changes\n\n- drop flag\n\n### Features\n\n- add export\n\n### Bug fixes\n\n- handle tabs' ]"
printf 'feat: add export\nfix: handle tabs\n' > "$W/c2.txt"
f=$($CLI --input "$W/c2.txt" --version 2.0.0)
check "file input unchanged without breaking" "[ \"\$f\" = \$'## 2.0.0\n\n### Features\n\n- add export\n\n### Bug fixes\n\n- handle tabs' ]"
echo "RESULT pass=$pass fail=$fail"
