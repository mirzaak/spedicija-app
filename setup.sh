#!/bin/bash
# Spedicija App — setup script
# Run once: bash setup.sh
# Then start with: bash start.sh

set -e

echo "======================================"
echo "  Spedicija App — instalacija"
echo "======================================"

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "GREŠKA: Python3 nije pronađen. Instaliraj Python 3.10+"
  exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Python verzija: $PYTHON_VERSION"

# Create virtualenv
if [ ! -d "venv" ]; then
  echo "Kreiram virtualenv..."
  python3 -m venv venv
fi

source venv/bin/activate

echo "Instaliram Python pakete..."
pip install --upgrade pip -q
pip install -r backend/requirements.txt

# Check Tesseract (optional, for scanned PDFs)
echo ""
if command -v tesseract &>/dev/null; then
  echo "✓ Tesseract OCR je pronađen: $(tesseract --version 2>&1 | head -1)"
else
  echo "⚠  Tesseract OCR nije pronađen."
  echo "   Za skenirane PDF fakture, instaliraj ga:"
  echo "   macOS:  brew install tesseract"
  echo "   Ubuntu: sudo apt install tesseract-ocr"
fi

# Create data directories
mkdir -p data/declarations data/uploads data/output

echo ""
echo "======================================"
echo "  Instalacija završena!"
echo "======================================"
echo ""
echo "Sljedeći koraci:"
echo "  1. Kopiraj XML deklaracije u: data/declarations/"
echo "  2. Pokretanje: bash start.sh"
echo "     ili: source venv/bin/activate && cd backend && uvicorn main:app --reload"
echo "  3. Otvori browser: http://localhost:8000"
