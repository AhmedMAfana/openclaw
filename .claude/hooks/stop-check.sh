#!/bin/bash
# Stop hook — fires when Claude is about to declare "done"
# Reminds Claude to verify if any Python files were modified

# Check if any .py files were modified in the git working tree
MODIFIED=$(git diff --name-only 2>/dev/null | grep '\.py$' | head -5)
UNTRACKED=$(git ls-files --others --exclude-standard 2>/dev/null | grep '\.py$' | head -5)

CHANGED="$MODIFIED $UNTRACKED"
CHANGED=$(echo "$CHANGED" | xargs)

if [ -n "$CHANGED" ]; then
    echo "STOP CHECK: Python files were modified but may not be verified:"
    echo "$CHANGED" | tr ' ' '\n' | head -10
    echo ""
    echo "Before declaring done, confirm:"
    echo "  1. All changed files pass py_compile"
    echo "  2. New handlers/modules are registered in their parent __init__.py or start_bot()"
    echo "  3. Function signatures match between caller and callee"
    echo "  4. If you cannot test end-to-end, say so explicitly"
fi

# Always exit 0 — this is advisory, not blocking
exit 0
