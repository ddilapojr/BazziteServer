#!/bin/bash

set -e

echo "-- Bazzite Integration Server Installer --"

# Ensure we are in the repo root
cd "$(dirname "$0")"
INSTALL_DIR="$(pwd)"

echo "Creating virtual environment"
rm -rf venv
python3 -m venv venv
source venv/bin/activate

echo "Installing Python dependencies"
pip install --upgrade pip
pip install --only-binary=:all: -r requirements.txt

echo "Setting up user systemd service"
mkdir -p ~/.config/systemd/user
sed "s|%h/bazzite-server|${INSTALL_DIR}|g" systemd/bazzite-server.service.user \
  > ~/.config/systemd/user/bazzite-server.service

echo "Reloading user systemd daemon"
systemctl --user daemon-reload

echo "Enabling and starting service"
systemctl --user enable --now bazzite-server

echo "Done!"
echo "To check status: systemctl --user status bazzite-server"
echo "To see logs: journalctl --user -u bazzite-server -f"
