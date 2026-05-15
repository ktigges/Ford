#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# Lightning Environment Setup Helper
# ============================================================================
# This script helps you set up the .env file with required configuration

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ROOT_DIR}/.env"
ENV_EXAMPLE="${ROOT_DIR}/.env.example"

echo "Lightning Environment Setup"
echo "============================"
echo ""

# Check if .env already exists
if [[ -f "${ENV_FILE}" ]]; then
    echo "✓ .env file already exists"
    echo ""
    read -p "Do you want to regenerate it? (y/N): " -r REGENERATE
    if [[ "${REGENERATE}" != "y" && "${REGENERATE}" != "Y" ]]; then
        echo "Keeping existing .env file"
        exit 0
    fi
fi

# Create .env from template
echo "Creating .env from template..."
cp "${ENV_EXAMPLE}" "${ENV_FILE}"
echo "✓ .env created"
echo ""

# Generate a secret key if needed
echo "Generating secure Flask secret key..."
SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
echo "Generated: ${SECRET_KEY}"
echo ""

# Update .env with the generated secret
sed -i "s|your-secure-secret-key-here|${SECRET_KEY}|" "${ENV_FILE}"
echo "✓ LIGHTNING_SECRET_KEY set in .env"
echo ""

# Show file location
echo "Configuration Complete!"
echo "======================"
echo "✓ Environment file created at: ${ENV_FILE}"
echo ""
echo "Next steps:"
echo "1. Review .env and configure any additional API keys as needed:"
echo "   - OPENWEATHER_API_KEY (optional, falls back to 'demo' mode)"
echo "   - GOOGLE_MAPS_API_KEY (optional, only if using Google routing)"
echo "   - OPENROUTESERVICE_API_KEY (optional, only if using ORS routing)"
echo ""
echo "2. Start the application:"
echo "   ./start.sh"
echo ""
