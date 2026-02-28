#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
REVISOR AUTOMÁTICO DE DOCUMENTOS PARA APOSTILLAS
Versión 4.0 – Con lógica de vinculación IF ↔ CE (Partidas de nacimiento GCABA)

MEJORAS EN ESTA VERSIÓN:
- Detecta automáticamente archivos IF y CE por nombre
- Vincula cada CE con su IF correspondiente verificando el número referenciado
- Combina ambos PDFs para análisis unificado con Claude
- La firma que importa es la del CE (no la del IF)
- Alerta si falta alguno de los dos archivos del par
- Fix: Detecta formato "26 de febrero de 2026" correctamente
- Fix: Prompt 100% dinámico (no hardcoded para ningún año específico)
"""

import os
import base64
import json
import re
import io
from datetime import datetime, timedelta
import streamlit as st
import anthropic
import pandas as pd
from PyPDF2 import PdfReader, PdfWriter
import pdfplumber
from openpyxl import load_workbook
from openpyxl.styles import PatternFill, Font, Alignment

st.set_page_config(page_title="Revisor de Apostillas", page_icon="📄", layout="centered")
st.title("📄 Revisor Automático de Apostillas")
st.markdown("Subí los PDFs y el sistema los analiza automáticamente con IA.")

CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not CLAUDE_API_KEY:
    CLAUDE_API_KEY = st.text_input("🔑 Ingresá tu API Key de Claude:", type="password", placeholder="sk-ant-api03-...")
if not CLAUDE_API_KEY:
    st.warning("Ingresá tu API Key para continuar.")
    st.stop()

# =============================================================================
# FIRMA DIGITAL - busca en los mismos lugares que Adobe Reader
# =============================================================================

def verificar_firma_digital(pdf_bytes):
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        firmas = []

        root = reader.trailer.get('/Root', {})
        if hasattr(root, 'get_object'):
            root = root.get_object()

        acroform = root.get('/AcroForm', None)
        if acroform:
            if hasattr(acroform, 'get_object'):
                acroform = acroform.get_object()
            fields = acroform.get('/Fields', [])
            for field_ref in fields:
                try:
                    field = field_ref.get_object()
                    if field.get('/FT') == '/Sig':
                        v = field.get('/V')
                        if v:
                            if hasattr(v, 'get_object'):
                                v = v.get_object()
                            nombre = str(v.get('/Name', ''))
                            razon = str(v.get('/Reason', ''))
                            firmante = nombre or razon or 'Firma digital detectada'
                            if firmante not in firmas:
                                firmas.append(firmante)
                except:
                    continue

        for page in reader.pages:
            try:
                if '/Annots' in page:
                    for annot_ref in page['/Annots']:
                        annot = annot_ref.get_object()
                        if annot.get('/Subtype') == '/Widget' and annot.get('/FT') == '/Sig':
                            v = annot.get('/V')
                            if v:
                                if hasattr(v, 'get_object'):
                                    v = v.get_object()
                                nombre = str(v.get('/Name', 'Firma en página'))
                                if nombre not in firmas:
                                    firmas.append(nombre)
            except:
                continue

        raw = str(reader.trailer)
        if "/Sig" in raw and not firmas:
            firmas.append("Firma digital detectada (certificado no extraíble)")

        return {"tiene_firma": bool(firmas), "cantidad_firmas": len(firmas), "firmantes": firmas}

    except:
        return {"tiene_firma": None, "cantidad_firmas": 0, "firmantes": []}

# =============================================================================
# LÓGICA IF ↔ CE
# =============================================================================

def extraer_clave_if(texto):
    """
    Extrae la clave única de un número IF: tupla (año, numero).
    Ejemplo: "IF-2015-29802485- -DGRC" → ("2015", "29802485")
    Acepta variantes con espacios extra o guiones entre los segmentos.
    """
    match = re.search(r'IF[\s\-_]+(\d{4})[\s\-_]+(\d+)', texto, re.IGNORECASE)
    if match:
        return (match.group(1), match.group(2))
    return None

def extraer_texto_pdf(pdf_bytes):
    """
    Extrae texto de un PDF usando pdfplumber con extract_words().
    extract_words() es mucho más robusto que extract_text() para PDFs de GCABA
    ya que no pierde líneas por problemas de encoding de fuentes.
    """
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            partes = []
            for page in pdf.pages:
                try:
                    words = page.extract_words()
                    if words:
                        partes.append(" ".join(w["text"] for w in words))
                except:
                    pass
            return " ".join(partes)
    except:
        return ""

def extraer_if_de_bytes_crudos(pdf_bytes):
    """
    Extrae el número IF directamente de los bytes crudos del PDF.
    Funciona incluso cuando el número está en una imagen escaneada,
    porque GEDO lo embebe también como texto en el stream interno del PDF.
    Ejemplo: "IF-2015-29802485- -DGRC" → ("2015", "29802485")
    """
    try:
        raw = pdf_bytes.decode("latin-1", errors="ignore")
        match = re.search(r'IF[-\s]+(\d{4})[-\s]+(\d+)', raw)
        if match:
            return (match.group(1), match.group(2))
    except:
        pass
    return None

def detectar_tipo_por_contenido(pdf_bytes, nombre_archivo=""):
    """
    Clasifica el PDF como CE, IF u OTRO leyendo su CONTENIDO.

    CE → se detecta por "CERTIFICO QUE EL PRESENTE DOCUMENTO" (inequívoco)
         + extrae el número IF referenciado desde el texto seleccionable.

    IF → se detecta por señales de GCABA en el texto.
         Su número IF se extrae de los bytes crudos del PDF (donde GEDO
         lo embebe aunque visualmente esté en la imagen escaneada).
         Esto permite emparejamiento EXACTO con el CE: si los números no
         coinciden, no se emparejan. Sin fallbacks ciegos.

    OTRO → sin señales reconocibles.

    Retorna: ("CE", clave_if_referenciada, texto_debug)
           | ("IF", clave_if_propia, texto_debug)   ← clave puede ser None si no se extrae
           | ("OTRO", None, texto_debug)
    """
    texto_raw = extraer_texto_pdf(pdf_bytes)
    texto_norm = re.sub(' +', ' ', texto_raw.replace('\n', ' ')).strip()
    texto_upper = texto_norm.upper()

    # ── Es CE: frase inequívoca de GCABA ────────────────────────────────────
    if "CERTIFICO QUE EL PRESENTE DOCUMENTO" in texto_upper:
        clave = extraer_clave_if(texto_norm)
        return ("CE", clave, texto_norm)

    # ── Es IF: señales de GCABA + extracción de número desde bytes crudos ───
    señales_gcaba = [
        "GOBIERNO DE LA CIUDAD",
        "HOJA ADICIONAL DE FIRMAS",
        "REGISTRO DEL ESTADO CIVIL",
        "GEDO",
    ]
    if any(s in texto_upper for s in señales_gcaba):
        clave_if = extraer_if_de_bytes_crudos(pdf_bytes)
        return ("IF", clave_if, texto_norm)

    # ── OTRO ─────────────────────────────────────────────────────────────────
    return ("OTRO", None, texto_norm)

def combinar_pdfs(pdf_bytes_lista):
    """Combina múltiples PDFs en uno solo. Retorna los bytes del PDF combinado."""
    writer = PdfWriter()
    for pdf_bytes in pdf_bytes_lista:
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            for page in reader.pages:
                writer.add_page(page)
        except Exception as e:
            st.warning(f"Advertencia al combinar PDF: {e}")
    output = io.BytesIO()
    writer.write(output)
    output.seek(0)
    return output.read()

# =============================================================================
# UTILIDADES
# =============================================================================

def pdf_a_base64(pdf_bytes):
    return base64.b64encode(pdf_bytes).decode('utf-8')

def calcular_dias_desde_fecha(fecha_str):
    if not fecha_str:
        return None
    fecha_str = fecha_str.lower()
    meses = {"enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,
             "julio":7,"agosto":8,"septiembre":9,"setiembre":9,"octubre":10,"noviembre":11,"diciembre":12}
    match = re.search(r'(\d{1,2})\s+de\s+([a-z]+)\s+(?:de(?:l)?\s+)?(\d{4})', fecha_str)
    if match:
        try:
            fecha = datetime(int(match.group(3)), meses.get(match.group(2), 0), int(match.group(1)))
            return (datetime.now() - fecha).days
        except:
            return None
    for f in ['%d/%m/%Y','%d-%m-%Y','%Y-%m-%d','%d/%m/%y','%d.%m.%Y']:
        try:
            return (datetime.now() - datetime.strptime(fecha_str, f)).days
        except:
            continue
    return None

# =============================================================================
# CLAUDE – Análisis individual
# =============================================================================

def analizar_con_claude(pdf_bytes):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    hoy = datetime.now().strftime('%d/%m/%Y')
    anio_actual = datetime.now().year
    mes_actual = datetime.now().strftime('%B')
    anio_pasado = anio_actual - 1
    fecha_hace_30_dias = (datetime.now() - timedelta(days=30)).strftime('%d/%m/%Y')
    fecha_hace_90_dias = (datetime.now() - timedelta(days=90)).strftime('%d/%m/%Y')
    fecha_ejemplo_futura = (datetime.now() + timedelta(days=30)).strftime('%d/%m/%Y')

    prompt = f"""Analizá este documento para apostilla en Cancillería Argentina.

🗓️ CONTEXTO TEMPORAL (actualizado automáticamente):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• HOY es: {hoy}
• Año ACTUAL: {anio_actual}
• Mes ACTUAL: {mes_actual}

⚠️ REGLAS SOBRE FECHAS - Lee con atención:

FECHAS VÁLIDAS (NO marcar como problema):
• Cualquier fecha del año {anio_actual} hasta hoy ({hoy})
• Fechas recientes de {anio_pasado} (últimos meses)
• Ejemplo: "{fecha_hace_30_dias}" (hace 30 días) = VÁLIDO ✓
• Ejemplo: "{fecha_hace_90_dias}" (hace 90 días) = VÁLIDO ✓

FECHAS PROBLEMÁTICAS (sí marcar como problema):
• Solo fechas FUTURAS (posteriores a {hoy})
• Ejemplo: "{fecha_ejemplo_futura}" = FUTURO (problemático) ✗

REGLA SIMPLE: 
Si fecha ≤ {hoy} → VÁLIDA, NO marcar problema
Si fecha > {hoy} → FUTURA, marcar problema

NO menciones "{anio_actual}" como algo raro o futuro - ES EL AÑO ACTUAL.

📋 INSTRUCCIONES DE EXTRACCIÓN:

Para calidad_imagen - usá SOLO estas palabras exactas:
• "alta" o "clara" o "nítida" → si se lee bien
• "baja" → si cuesta leer pero se puede
• "borrosa" → si hay desenfoque notable
• "ilegible" → si no se puede leer

Para multiples_firmas:
• Marcá true SOLO si hay firmas de distintas autoridades que generan confusión real sobre cuál es la válida
• Si hay una sola firma clara, marcá false

Para problemas_detectados:
• Listá SOLO problemas concretos y reales
• NO incluyas la fecha como problema si es de {anio_actual}
• Si el documento está bien, dejá la lista vacía []

Para observacion_redactada:
• Escribí UNA sola oración clara y profesional que resuma el documento
• Ejemplo: "Certificado de antecedentes penales emitido el 15/02/{anio_actual} con firma digital de Juan Pérez, vigente."
• NO uses jerga técnica ni listes campos
• Si hay un problema REAL (no la fecha), mencionalo al final

Para titular_documento:
• El nombre completo de la persona a quien pertenece el documento
• Buscá el nombre en TODO el documento, incluso manuscrito o en anotaciones marginales
• En acta de nacimiento: nombre del nacido (ej: "Joel Lautaro Sueldo")
• En antecedente penal: nombre del solicitante
• En título: nombre del graduado
• Campo OBLIGATORIO, nunca vacío si el nombre aparece

Campos a extraer (JSON válido):
{{
  "tipo_documento": string,
  "titular_documento": string,
  "fecha_emision": string (tal como aparece),
  "anio_documento": number,
  "es_pre_2012": boolean,
  "firmantes_visibles": [strings],
  "cantidad_firmas_visibles": number,
  "multiples_firmas": boolean,
  "sello_ministerio_visible": boolean,
  "sello_claro": boolean,
  "calidad_imagen": "alta"|"clara"|"nítida"|"baja"|"borrosa"|"ilegible",
  "es_foto_celular": boolean,
  "problemas_detectados": [strings vacía si todo OK],
  "observacion_redactada": string
}}"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_a_base64(pdf_bytes)}},
            {"type": "text", "text": prompt}
        ]}]
    )

    respuesta = message.content[0].text.strip()
    respuesta = re.sub(r'^```json\n?', '', respuesta)
    respuesta = re.sub(r'\n?```$', '', respuesta)
    return json.loads(respuesta)

# =============================================================================
# CLAUDE – Análisis de PAR IF + CE (PDF combinado)
# =============================================================================

def analizar_par_if_ce_con_claude(if_bytes, ce_bytes, nombre_if, nombre_ce):
    """
    Combina el IF y el CE en un solo PDF y lo envía a Claude para análisis unificado.
    La firma que importa es la del CE. El IF es el documento original.
    """
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    hoy = datetime.now().strftime('%d/%m/%Y')
    anio_actual = datetime.now().year

    # Combinar ambos PDFs en uno
    pdf_combinado = combinar_pdfs([if_bytes, ce_bytes])

    prompt = f"""Estás analizando un PAR de documentos vinculados para apostilla en Cancillería Argentina.

📂 DOCUMENTO 1 (páginas iniciales): Archivo IF – Es el ACTA o documento original (ej: acta de nacimiento).
📂 DOCUMENTO 2 (páginas siguientes): Archivo CE – Es el CERTIFICADO que avala al IF.

HOY: {hoy} | AÑO ACTUAL: {anio_actual}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔎 TU TAREA PRINCIPAL: Verificar la vinculación IF ↔ CE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

El CE debe contener en su texto la frase:
"Número/s de documento/s electrónico/s: [número IF]"

El número IF del primer archivo es: {nombre_if}

Verificá si el CE (segundo documento) hace referencia a ese número IF en su texto.

⚠️ IMPORTANTE SOBRE FIRMAS:
• La firma que IMPORTA para apostilla es la del CE (segundo documento), NO la del IF.
• El IF puede tener firma ológrafa (manuscrita) o sellos – eso es NORMAL, no es un problema.
• Evaluá la firma del CE: debe ser digital/electrónica, emitida por GCABA (DGRC).

🖊️ CÓMO EXTRAER EL FIRMANTE DEL CE – Lee con mucha atención:
Los documentos CE de GCABA tienen un bloque de firma que dice:
  "Digitally signed by Comunicaciones Oficiales"
  "Date: YYYY.MM.DD HH:MM:SS"
  
  [Nombre Apellido]         ← ESTE es el firmante_ce que querés
  [Cargo]
  [Organismo]

• "Comunicaciones Oficiales" NO es el firmante. Es el sistema técnico que certifica.
• El firmante real es el NOMBRE HUMANO que aparece DEBAJO del bloque "Digitally signed".
• Ejemplo: si ves "Gonzalo Alvarez / Gerente Operativo / D.G.REG.ESTADO CIVIL..." → firmante_ce = "Gonzalo Alvarez"
• Puede haber dos bloques "Digitally signed" en el CE (uno arriba y uno abajo). En ambos casos el nombre humano aparece debajo. Tomá el primero que encuentres con nombre legible.
• Si no encontrás ningún nombre humano → firmante_ce = "No identificado"

Para calidad_imagen - usá SOLO: "alta", "clara", "nítida", "baja", "borrosa" o "ilegible"

Para titular_documento:
• El nombre completo de la persona del ACTA (IF), ej: "Apolo Luciano Arce Chumbi"
• Buscá en el documento manuscrito o impreso

Para fecha_emision:
• Usá la fecha del CE (no la del IF original), porque el CE es el que tiene vigencia actual

Para observacion_redactada:
• Una sola oración que resuma el par: tipo de acta, titular, si el CE vincula correctamente al IF, y quién firmó el CE.
• Ejemplo: "Acta de nacimiento de Apolo Luciano Arce Chumbi, CE emitido el 20/02/2026, firmado por Gonzalo Alvarez, referencia IF verificada correctamente."

Respondé SOLO JSON válido:
{{
  "tipo_documento": string,
  "titular_documento": string,
  "fecha_emision": string,
  "anio_documento": number,
  "es_pre_2012": boolean,
  "firmantes_visibles": [strings],
  "cantidad_firmas_visibles": number,
  "multiples_firmas": boolean,
  "sello_ministerio_visible": boolean,
  "sello_claro": boolean,
  "calidad_imagen": "alta"|"clara"|"nítida"|"baja"|"borrosa"|"ilegible",
  "es_foto_celular": boolean,
  "ce_referencia_if_correctamente": boolean,
  "numero_if_encontrado_en_ce": string,
  "firmante_ce": string,
  "cargo_firmante_ce": string,
  "problemas_detectados": [],
  "observacion_redactada": string
}}"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_a_base64(pdf_combinado)}},
            {"type": "text", "text": prompt}
        ]}]
    )

    respuesta = message.content[0].text.strip()
    respuesta = re.sub(r'^```json\n?', '', respuesta)
    respuesta = re.sub(r'\n?```$', '', respuesta)
    return json.loads(respuesta)

# =============================================================================
# LÓGICA NORMATIVA
# =============================================================================

def evaluar_documento(firma_info, analisis):
    tipo = (analisis.get('tipo_documento') or "").lower()
    estado = "✅ OK"
    accion = "Listo para cargar"
    problemas = []

    calidad = (analisis.get("calidad_imagen") or "").lower()
    problemas_claude = analisis.get("problemas_detectados") or []

    # PENAL: vencimiento 90 días
    if "antecedente" in tipo or "penal" in tipo:
        fecha = analisis.get('fecha_emision')
        if not fecha:
            estado = "⚠️ REVISAR"
            accion = "No se detectó fecha"
            problemas.append("No se pudo leer la fecha de emisión")
        else:
            dias = calcular_dias_desde_fecha(fecha)
            
            if dias is None:
                estado = "⚠️ REVISAR"
                accion = "Fecha no interpretable"
                problemas.append(f"No se pudo interpretar la fecha: {fecha}")
            
            elif dias < 0:
                estado = "⚠️ REVISAR"
                accion = "Fecha posterior a hoy"
                problemas.append(f"Fecha futura detectada: {fecha} (verificar si es error de sistema)")
            
            elif dias > 90:
                estado = "❌ RECHAZAR"
                accion = "Certificado vencido (>90 días)"
                problemas.append(f"Vencido hace {dias} días (máximo: 90)")
            
            else:
                estado = "✅ OK"
                accion = "Certificado vigente"

        if firma_info['tiene_firma'] == False:
            estado = "❌ RECHAZAR"
            accion = "Falta firma digital"
            problemas.append("No se detectó firma digital")

    # Títulos y analíticos
    if "título" in tipo or "analítico" in tipo:
        if analisis.get('cantidad_firmas_visibles', 0) == 0:
            estado = "⚠️ REVISAR"
            accion = "No se detecta firma visible"
            problemas.append("Sin firma visible")

    # Múltiples firmas
    if analisis.get('multiples_firmas') and firma_info['cantidad_firmas'] > 1:
        if estado == "✅ OK":
            estado = "⚠️ REVISAR"
            accion = "Verificar cuál firma corresponde"
        problemas.append("Múltiples firmas detectadas")

    # Calidad
    if calidad == "ilegible":
        estado = "❌ RECHAZAR"
        accion = "Imagen ilegible"
        problemas.append("Imagen ilegible")
    elif calidad in ["baja", "borrosa"]:
        if estado == "✅ OK":
            estado = "⚠️ REVISAR"
            accion = "Calidad de imagen insuficiente"
        problemas.append(f"Calidad de imagen: {calidad}")

    # Problemas detectados por Claude (filtrar falsos positivos de fecha)
    problemas_filtrados = []
    for p in problemas_claude:
        p_lower = p.lower()
        anio_actual = str(datetime.now().year)
        if anio_actual not in p_lower and "fecha futura" not in p_lower and "fecha posterior" not in p_lower:
            problemas_filtrados.append(p)
    
    if problemas_filtrados:
        if estado == "✅ OK":
            estado = "⚠️ REVISAR"
            accion = "Revisar problemas detectados"
        for p in problemas_filtrados:
            problemas.append(p)

    # Foto de celular
    if analisis.get("es_foto_celular"):
        if estado == "✅ OK":
            estado = "⚠️ REVISAR"
            accion = "Documento fotografiado con celular"
        problemas.append("Documento fotografiado con celular")

    return estado, accion, problemas

def evaluar_par_if_ce(firma_info_ce, analisis_par):
    """
    Evalúa el resultado del análisis de un par IF+CE.
    La firma que importa es la del CE.
    """
    estado = "✅ OK"
    accion = "Par IF+CE válido – Listo para cargar"
    problemas = []

    # Verificación principal: ¿el CE hace referencia al IF?
    if not analisis_par.get("ce_referencia_if_correctamente"):
        estado = "❌ RECHAZAR"
        accion = "El CE no referencia al IF correspondiente"
        problemas.append("El CE no contiene el número IF correcto en su texto")

    # Firma del CE
    if firma_info_ce['tiene_firma'] == False:
        estado = "❌ RECHAZAR"
        accion = "CE sin firma digital"
        problemas.append("El CE no tiene firma digital válida")
    elif firma_info_ce['tiene_firma'] is None:
        if estado == "✅ OK":
            estado = "⚠️ REVISAR"
            accion = "Firma del CE no detectada automáticamente"
        problemas.append("No se pudo verificar firma digital del CE automáticamente")

    # Calidad
    calidad = (analisis_par.get("calidad_imagen") or "").lower()
    if calidad == "ilegible":
        estado = "❌ RECHAZAR"
        accion = "Imagen ilegible"
        problemas.append("Imagen ilegible")
    elif calidad in ["baja", "borrosa"]:
        if estado == "✅ OK":
            estado = "⚠️ REVISAR"
            accion = "Calidad de imagen insuficiente"
        problemas.append(f"Calidad de imagen: {calidad}")

    # Problemas detectados por Claude (filtrar falsos positivos)
    problemas_claude = analisis_par.get("problemas_detectados") or []
    anio_actual = str(datetime.now().year)
    for p in problemas_claude:
        p_lower = p.lower()
        if anio_actual not in p_lower and "fecha futura" not in p_lower and "fecha posterior" not in p_lower:
            if estado == "✅ OK":
                estado = "⚠️ REVISAR"
                accion = "Revisar problemas detectados"
            problemas.append(p)

    return estado, accion, problemas

def generar_observacion(analisis, problemas):
    obs_base = analisis.get("observacion_redactada") or analisis.get("observaciones") or ""
    if problemas:
        extra = "; ".join(problemas)
        if obs_base:
            return f"{obs_base.strip()} — {extra}"
        return extra
    return obs_base.strip()

# =============================================================================
# EXCEL EN MEMORIA
# =============================================================================

def generar_excel(df):
    output = io.BytesIO()
    df.to_excel(output, index=False, engine="openpyxl")
    output.seek(0)
    wb = load_workbook(output)
    ws = wb.active

    verde = PatternFill(start_color="E6F4EA", end_color="E6F4EA", fill_type="solid")
    amarillo = PatternFill(start_color="FFF8E1", end_color="FFF8E1", fill_type="solid")
    rojo = PatternFill(start_color="FDECEA", end_color="FDECEA", fill_type="solid")
    gris = PatternFill(start_color="ECECEC", end_color="ECECEC", fill_type="solid")

    for cell in ws[1]:
        cell.fill = gris
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")

    col_estado = next((i for i, c in enumerate(ws[1], 1) if c.value == "Estado"), None)

    for row in ws.iter_rows(min_row=2):
        estado = row[col_estado - 1].value if col_estado else ""
        fill = verde if "OK" in str(estado) else amarillo if "REVISAR" in str(estado) else rojo if "RECHAZAR" in str(estado) else None
        if fill:
            for cell in row:
                cell.fill = fill

    for col in ws.columns:
        max_len = max((len(str(c.value)) for c in col if c.value), default=0)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 3, 60)

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    final = io.BytesIO()
    wb.save(final)
    final.seek(0)
    return final

# =============================================================================
# INTERFAZ
# =============================================================================

archivos = st.file_uploader("📂 Subí los PDFs a revisar", type=["pdf"], accept_multiple_files=True)

if archivos:
    st.info(f"{len(archivos)} archivo(s) cargado(s). Listo para procesar.")

    if st.button("🚀 Analizar documentos", type="primary"):
        resultados = []
        barra = st.progress(0)
        estado_texto = st.empty()

        # ─────────────────────────────────────────────────────────────────
        # PASO 1: Clasificar archivos en IF, CE y OTROS leyendo el CONTENIDO
        # (independiente del nombre del archivo)
        # ─────────────────────────────────────────────────────────────────
        archivos_if = {}   # clave_if (año, numero) → {"archivo": ..., "bytes": ..., "nombre": ...}
        archivos_ce = {}   # nombre → {"archivo": ..., "bytes": ..., "clave_if_ref": ...}
        archivos_otros = []

        # Panel de debug expandible
        with st.expander("🔍 Debug: clasificación de archivos (expandí si hay problemas)", expanded=False):
            debug_placeholder = st.empty()
            debug_rows = []

        for archivo in archivos:
            pdf_bytes = archivo.read()
            tipo, clave, texto_extraido = detectar_tipo_por_contenido(pdf_bytes, archivo.name)

            # Info de debug
            preview = texto_extraido[:300].replace("\n", " ") if texto_extraido else "(sin texto extraíble)"
            clave_str = f"IF-{clave[0]}-{clave[1]}" if clave else "—"
            debug_rows.append({
                "Archivo": archivo.name,
                "Clasificado como": tipo,
                "Clave IF detectada": clave_str,
                "Texto extraído (primeros 300 chars)": preview
            })
            debug_placeholder.dataframe(pd.DataFrame(debug_rows), use_container_width=True)

            if tipo == "IF":
                archivos_if[clave] = {"archivo": archivo, "bytes": pdf_bytes, "nombre": archivo.name}

            elif tipo == "CE":
                archivos_ce[archivo.name] = {
                    "archivo": archivo,
                    "bytes": pdf_bytes,
                    "clave_if_ref": clave,
                    "nombre": archivo.name
                }
            else:
                archivos_otros.append({"archivo": archivo, "bytes": pdf_bytes})

        # ─────────────────────────────────────────────────────────────────
        # PASO 2: Emparejar CE ↔ IF — solo por coincidencia EXACTA de número
        # No hay fallbacks ciegos: si los números no coinciden, no se emparejan
        # ─────────────────────────────────────────────────────────────────
        pares = []
        if_usados = set()
        ce_usados = set()

        for ce_nombre, ce_data in archivos_ce.items():
            clave_ref = ce_data["clave_if_ref"]

            if clave_ref and clave_ref in archivos_if:
                # Coincidencia exacta: el CE referencia este IF por número
                pares.append({
                    "if": archivos_if[clave_ref],
                    "ce": ce_data
                })
                if_usados.add(clave_ref)
                ce_usados.add(ce_nombre)
            else:
                # Sin coincidencia: CE huérfano (falta el IF o los números no coinciden)
                ce_usados.add(ce_nombre)
                archivos_otros.append({
                    "archivo": ce_data["archivo"],
                    "bytes": ce_data["bytes"],
                    "advertencia_ce_sin_if": True,
                    "clave_if_ref": clave_ref
                })

        # IFs que no fueron referenciados por ningún CE
        for clave_if, if_data in archivos_if.items():
            if clave_if not in if_usados:
                archivos_otros.append({
                    "archivo": if_data["archivo"],
                    "bytes": if_data["bytes"],
                    "advertencia_if_sin_ce": True
                })

        total_tareas = len(pares) + len(archivos_otros)
        tarea_actual = 0

        # ─────────────────────────────────────────────────────────────────
        # PASO 3: Procesar PARES IF+CE
        # ─────────────────────────────────────────────────────────────────
        for par in pares:
            if_data = par["if"]
            ce_data = par["ce"]
            nombre_display = f"{if_data['nombre']} + {ce_data['nombre']}"
            estado_texto.text(f"Analizando par: {nombre_display}...")

            try:
                # Firma: solo la del CE importa
                firma_info_ce = verificar_firma_digital(ce_data["bytes"])

                # Análisis conjunto con Claude (PDF combinado)
                analisis_par = analizar_par_if_ce_con_claude(
                    if_data["bytes"],
                    ce_data["bytes"],
                    if_data["nombre"],
                    ce_data["nombre"]
                )

                estado, accion, problemas = evaluar_par_if_ce(firma_info_ce, analisis_par)
                observacion = generar_observacion(analisis_par, problemas)

                tiene_firma = firma_info_ce["tiene_firma"]
                firma_texto = "SÍ" if tiene_firma else ("NO" if tiene_firma == False else "NO DETECTADA")

                firmante_ce = analisis_par.get("firmante_ce", "") or "No identificado"
                cargo_ce = analisis_par.get("cargo_firmante_ce", "") or ""
                firmante_display = f"{firmante_ce} ({cargo_ce})" if cargo_ce else firmante_ce

                resultados.append({
                    "Archivo": nombre_display,
                    "Tipo trámite": "📎 Par IF+CE",
                    "Titular": analisis_par.get("titular_documento"),
                    "Tipo": analisis_par.get("tipo_documento"),
                    "Fecha CE": analisis_par.get("fecha_emision"),
                    "CE referencia IF": "✅ SÍ" if analisis_par.get("ce_referencia_if_correctamente") else "❌ NO",
                    "IF encontrado en CE": analisis_par.get("numero_if_encontrado_en_ce", ""),
                    "Firmante CE": firmante_display,
                    "Firma Digital CE": firma_texto,
                    "Firmantes Certificado": ", ".join(firma_info_ce["firmantes"]),
                    "Estado": estado,
                    "Acción": accion,
                    "Observaciones": observacion
                })

            except Exception as e:
                resultados.append({
                    "Archivo": nombre_display,
                    "Tipo trámite": "📎 Par IF+CE",
                    "Titular": "",
                    "Tipo": "",
                    "Fecha CE": "",
                    "CE referencia IF": "",
                    "IF encontrado en CE": "",
                    "Firmante CE": "",
                    "Firma Digital CE": "",
                    "Firmantes Certificado": "",
                    "Estado": "⚠️ REVISAR",
                    "Acción": "Error de análisis",
                    "Observaciones": f"Error: {str(e)}"
                })

            tarea_actual += 1
            barra.progress(tarea_actual / total_tareas)

        # ─────────────────────────────────────────────────────────────────
        # PASO 4: Procesar archivos individuales (OTROS, IF sin CE, CE sin IF)
        # ─────────────────────────────────────────────────────────────────
        for item in archivos_otros:
            archivo = item["archivo"]
            pdf_bytes = item["bytes"]
            estado_texto.text(f"Analizando {archivo.name}...")

            advertencia_extra = ""
            if item.get("advertencia_if_sin_ce"):
                advertencia_extra = "⚠️ IF sin CE correspondiente cargado"
            elif item.get("advertencia_ce_sin_if"):
                clave_ref = item.get("clave_if_ref")
                ref_str = f"IF-{clave_ref[0]}-{clave_ref[1]}" if clave_ref else "desconocido"
                advertencia_extra = f"⚠️ CE sin IF correspondiente (busca: {ref_str})"

            try:
                firma_info = verificar_firma_digital(pdf_bytes)
                analisis = analizar_con_claude(pdf_bytes)
                estado, accion, problemas = evaluar_documento(firma_info, analisis)

                if advertencia_extra:
                    problemas.append(advertencia_extra)
                    if estado == "✅ OK":
                        estado = "⚠️ REVISAR"
                        accion = advertencia_extra

                observacion = generar_observacion(analisis, problemas)

                tiene_firma = firma_info["tiene_firma"]
                firma_texto = "SÍ" if tiene_firma else ("NO" if tiene_firma == False else "NO DETECTADA")

                resultados.append({
                    "Archivo": archivo.name,
                    "Tipo trámite": "📄 Individual",
                    "Titular": analisis.get("titular_documento"),
                    "Tipo": analisis.get("tipo_documento"),
                    "Fecha CE": analisis.get("fecha_emision"),
                    "CE referencia IF": "—",
                    "IF encontrado en CE": "—",
                    "Firmante CE": "—",
                    "Firma Digital CE": firma_texto,
                    "Firmantes Certificado": ", ".join(firma_info["firmantes"]),
                    "Estado": estado,
                    "Acción": accion,
                    "Observaciones": observacion
                })

            except Exception as e:
                resultados.append({
                    "Archivo": archivo.name,
                    "Tipo trámite": "📄 Individual",
                    "Titular": "",
                    "Tipo": "",
                    "Fecha CE": "",
                    "CE referencia IF": "—",
                    "IF encontrado en CE": "—",
                    "Firmante CE": "—",
                    "Firma Digital CE": "",
                    "Firmantes Certificado": "",
                    "Estado": "⚠️ REVISAR",
                    "Acción": "Error de análisis",
                    "Observaciones": f"Error: {str(e)}"
                })

            tarea_actual += 1
            barra.progress(tarea_actual / total_tareas)

        estado_texto.text("✅ Análisis completado.")

        # Resumen de pares detectados
        if pares:
            st.success(f"🔗 {len(pares)} par(es) IF+CE vinculados correctamente.")

        df = pd.DataFrame(resultados)

        st.subheader("📊 Resultados")
        st.dataframe(df, use_container_width=True)

        st.download_button(
            label="⬇️ Descargar Excel",
            data=generar_excel(df),
            file_name="revision_apostillas.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

st.markdown("---")
st.markdown("<p style='text-align: center; color: gray; font-size: 12px;'>Desarrollado por Leandro Spinelli · Automatización de procesos documentales con IA · 2026</p>", unsafe_allow_html=True)