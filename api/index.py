import os
import sys

# La raíz del proyecto debe estar en el path para importar el paquete "backend"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.main import app  # noqa: E402,F401  (Vercel busca la variable "app")