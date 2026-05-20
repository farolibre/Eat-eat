# main.py
import os
import io
import re
import json
import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from PIL import Image
import pytesseract
from pydantic import BaseModel, Field, field_validator

# 🔧 Configuración y Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# Detectar Tesseract automáticamente o usar variable de entorno
# 🔍 Configuración de Tesseract OCR (Windows + Linux/Render)
import shutil
# Intenta detectar tesseract en el PATH del sistema (funciona en Linux/Render y Windows si está instalado)
tesseract_path = shutil.which("tesseract")
if tesseract_path:
    pytesseract.pytesseract.tesseract_cmd = tesseract_path
else:
    # Fallback para Windows: ruta típica de instalación
    windows_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(windows_path):
        pytesseract.pytesseract.tesseract_cmd = windows_path
    else:
        # Último recurso: usar "tesseract" y esperar que el sistema lo encuentre
        pytesseract.pytesseract.tesseract_cmd = "tesseract"

HISTORIAL_FILE = Path("historial.json")
_lock = threading.Lock()  # Evita corrupción por concurrencia

# 📦 Modelos de validación
class Producto(BaseModel):
    nombre: str
    cantidad: int = 1
    
    @field_validator('cantidad', mode='before')
    @classmethod
    def validar_cantidad(cls, v):
        try:
            return int(v) if v is not None else 1
        except (ValueError, TypeError):
            return 1

class EscanerResponse(BaseModel):
    productos: List[Producto]
    error: Optional[str] = None

# 🗄️ Gestión segura del historial (escritura atómica + thread-safe)
def cargar_historial() -> List[dict]:
    if not HISTORIAL_FILE.exists():
        return []
    with _lock:
        try:
            with open(HISTORIAL_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            logger.warning("historial.json corrupto o vacío. Reiniciando.")
            return []

def guardar_historial(historial: List[dict]) -> None:
    with _lock:
        tmp_file = HISTORIAL_FILE.with_suffix(".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(historial, f, indent=2, ensure_ascii=False)
            tmp_file.replace(HISTORIAL_FILE)  # Renombrado atómico (evita corrupción)
        except Exception as e:
            logger.error(f"Error guardando historial: {e}")
            if tmp_file.exists():
                tmp_file.unlink()
            raise HTTPException(status_code=500, detail="Error interno al guardar")

# 🚀 Aplicación FastAPI
app = FastAPI(title="Eat-eat API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 🔒 Cambia por tu dominio en producción
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("templates", exist_ok=True)
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})
@app.post("/escanear")
async def escanear(file: UploadFile = File(...)):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="El archivo debe ser una imagen")
    
    try:
        contents = await file.read()
        if len(contents) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Imagen demasiado grande")

        with Image.open(io.BytesIO(contents)) as img:
            # PSM 6 = bloque de texto uniforme (ideal para tickets)
            texto = pytesseract.image_to_string(img, lang="spa+eng", config="--psm 6")

        print(f"--- TEXTO OCR ---\n{texto}\n------------------")

        productos = []
        seen = set()
        # Palabras que indican que la línea NO es un producto
        ignorar = {"total", "efectivo", "tarjeta", "debito", "credito", "iva", "vuelto", 
                   "cambio", "fecha", "hora", "ticket", "compra", "sucursal", "caja", 
                   "gracias", "tienda", "cajero", "operador", "importe", "subtotal",
                   "descuento", "oferta", "pvp", "uds", "unidades", "precio", "euros", "€",
                   "reciclable", "envase", "bolsa", "gadis", "ifa"}

        for linea in texto.splitlines():
            linea = linea.strip()
            if len(linea) < 3:
                continue

            # 1️⃣ Quitar precios al final (ej: "3,50", "12.99€", "1,20")
            linea = re.sub(r'\s+\d+[,\.\d]*\s*€?\s*$', '', linea).strip()
            
            # 2️⃣ Quitar pesos/medidas al final (ej: "500 G.", "1 KG", "200 ML")
            linea = re.sub(r'\s+\d*\s*[GgKkMmLl]+\.*\s*$', '', linea).strip()
            
            # 3️⃣ Limpiar puntuación OCR (puntos, comas, paréntesis, barras)
            linea = re.sub(r'[^\w\sáéíóúüñÁÉÍÓÚÜÑ\-]', ' ', linea)
            linea = re.sub(r'\s+', ' ', linea).strip()
            
            if len(linea) < 2 or len(linea) > 60:
                continue

            # 4️ Filtrar líneas de ruido o cabeceras/pies
            linea_lower = linea.lower()
            if any(p in linea_lower for p in ignorar) or linea_lower in seen:
                continue
                        # Descartar líneas que son solo números o símbolos
            # (Corrección: eliminados \j y \> que daban error de sintaxis)
            if re.match(r'^[\d\s\.\,\-\(\)\[\]\/\$\:\;\?\!\&]+$', linea):
                continue

            # 5️ Extraer cantidad si está al inicio (ej: "2 MANZANAS")
            cantidad = 1
            match_cant = re.match(r'^(\d{1,2})\s+', linea)
            if match_cant:
                cantidad = int(match_cant.group(1))
                linea = linea[match_cant.end():].strip()

            seen.add(linea_lower)
            productos.append({"nombre": linea.title(), "cantidad": cantidad})

        if not productos:
            return {"productos": [], "debug": f"Tesseract leyó: '{texto[:200]}...'"}

        # Ordenar por longitud (los más largos suelen ser los nombres completos)
        productos.sort(key=lambda x: len(x["nombre"]), reverse=True)
        return {"productos": productos[:25]}

    except Exception as e:
        logger.exception("Error en OCR")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@app.post("/guardar")
async def guardar_compra(productos: List[Producto]):
    print(f"📥 Recibidos {len(productos)} productos")  # 👈 Para ver en terminal
    
    if not productos:
        return {"ok": True, "mensaje": "Lista vacía"}
    
    try:
        historial = cargar_historial()
        productos_dict = [p.model_dump() if hasattr(p, 'model_dump') else p.__dict__ for p in productos]
        
        historial.append({
            "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "productos": productos_dict
        })
        guardar_historial(historial)
        return {"ok": True, "guardados": len(productos)}
    except Exception as e:
        print(f"❌ Error: {e}")
        return {"ok": False, "error": str(e)}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error guardando compra")
        raise HTTPException(status_code=500, detail="Error al persistir en historial")
@app.get("/historial")
async def get_historial():
    return cargar_historial()

from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory="."), name="static")  # 👈 Cambiado a /static

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)