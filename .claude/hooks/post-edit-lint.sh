#!/bin/bash
# PostToolUse hook — runs after every Edit/Write on a .py file
# Compiles the file to catch syntax errors immediately

# The tool result is piped via stdin as JSON. Extract the file path.
FILE=$(cat | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    path = data.get('file_path', '') or data.get('filePath', '')
    print(path)
except:
    print('')
" 2>/dev/null)

# Only check Python files
if [[ "$FILE" == *.py ]]; then
    if ! python3 -m py_compile "$FILE" 2>&1; then
        echo "SYNTAX ERROR in $FILE — fix before continuing"
        exit 2  # exit 2 = block + show message to Claude
    fi
fi

exit 0
