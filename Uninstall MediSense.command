#!/bin/bash
# Double-click to remove MediSense from this Mac (your recordings are kept
# unless you say otherwise). If macOS says it cannot be opened, right-click
# this file, choose Open, then Open again.
cd "$(dirname "$0")" && bash ./uninstall.sh
echo
read -n 1 -s -r -p "Press any key to close this window."
