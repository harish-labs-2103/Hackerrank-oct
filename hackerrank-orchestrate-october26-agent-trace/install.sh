#!/usr/bin/env bash
# Data Investigator — One-line curl installer
# Usage: curl -sSL <URL>/install.sh | bash
set -euo pipefail

echo "============================================================"
echo " Installing Data Investigator CLI..."
echo "============================================================"

# Ensure Python 3
if ! command -v python3 &>/dev/null; then
    echo "Error: Python 3 is required. Please install python3 (>=3.10) and pip." >&2
    exit 1
fi

INSTALL_DIR="${HOME}/.data-investigator"

if [ -d "${INSTALL_DIR}" ]; then
    echo "Updating existing installation at ${INSTALL_DIR}..."
    cd "${INSTALL_DIR}"
    git pull origin main || true
else
    echo "Cloning into ${INSTALL_DIR}..."
    git clone https://github.com/harish-labs-2103/Hackerrank-oct.git "${INSTALL_DIR}"
fi

WORK_DIR="${INSTALL_DIR}/hackerrank-orchestrate-october26-agent-trace"
cd "${WORK_DIR}"

echo "Installing dependencies..."
python3 -m pip install -r code/requirements.txt
python3 -m pip install -e code

echo ""
echo "Installation successful!"
echo "You can now run: data-investigator --help"
