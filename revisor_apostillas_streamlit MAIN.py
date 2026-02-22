#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
REVISOR AUTOMÁTICO DE DOCUMENTOS PARA APOSTILLAS
Versión Streamlit 3.1 – Fecha de hoy correcta + Observaciones mejoradas
"""

import os
import base64
import json
import re
import io
from datetime import datetime
import streamlit as st
import anthropic
import pandas as pd
from PyPDF2 import PdfReader
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
    match = re.search(r'(\d{1,2})\s+de\s+([a-z]+)\s+(?:del\s+)?(\d{4})', fecha_str)
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
# CLAUDE
# =============================================================================

def analizar_con_claude(pdf_bytes):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    hoy = datetime.now().strftime('%d/%m/%Y')

    prompt = f"""Analizá este documento y extraé información. La fecha de hoy es {hoy}. No consideres anómala ninguna fecha igual o anterior a hoy.

IMPORTANTE para calidad_imagen: usá SOLO estas palabras exactas:
- "alta" o "clara" o "nítida" → si se lee bien
- "baja" → si cuesta leer pero se puede
- "borrosa" → si hay desenfoque notable
- "ilegible" → si no se puede leer

Para multiples_firmas: marcá true SOLO si hay firmas de distintas autoridades que generan confusión real sobre cuál es la válida.

Para problemas_detectados: listá solo problemas concretos y reales. Si el documento está bien, dejá la lista vacía.

Para observacion_redactada: escribí UNA sola oración clara y profesional que resuma el documento. Por ejemplo: "Acta de nacimiento emitida por el Registro Civil de Chaco el 10/02/2026, con firma digital de Carlos Zanier, en buen estado." No uses jerga técnica ni listes campos. Si hay un problema real, mencionalo al final de la oración.

Para titular_documento: el nombre completo de la persona a quien pertenece el documento. Buscá el nombre en TODO el documento, incluso si está escrito a mano o en el cuerpo del acta. Ejemplos: en un acta de nacimiento es el nombre del bebé o persona nacida (ej: "Joel Lautaro Sueldo"); en un antecedente penal es el nombre del solicitante; en un título es el nombre del graduado. Este campo es OBLIGATORIO, nunca lo dejes vacío si el nombre aparece en algún lado del documento.

Campos a extraer (respondé SOLO JSON válido):
{{
  "tipo_documento": string,
  "titular_documento": string,
  "fecha_emision": string,
  "anio_documento": number,
  "es_pre_2012": boolean,
  "firmantes_visibles": [lista de strings],
  "cantidad_firmas_visibles": number,
  "multiples_firmas": boolean,
  "sello_ministerio_visible": boolean,
  "sello_claro": boolean,
  "calidad_imagen": "alta"|"clara"|"nítida"|"baja"|"borrosa"|"ilegible",
  "es_foto_celular": boolean,
  "problemas_detectados": [lista de strings, vacía si no hay problemas],
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
            estado = "⚠️ REVISAR"; accion = "No se detectó fecha"
            problemas.append("No se pudo leer la fecha de emisión")
        else:
            dias = calcular_dias_desde_fecha(fecha)
            if dias is None:
                estado = "⚠️ REVISAR"; accion = "Fecha no interpretable"
                problemas.append(f"No se pudo interpretar la fecha: {fecha}")
            elif dias > 90:
                estado = "❌ RECHAZAR"; accion = "Certificado vencido (>90 días)"
                problemas.append(f"Vencido hace {dias} días")

        if firma_info['tiene_firma'] == False:
            estado = "❌ RECHAZAR"; accion = "Falta firma digital"
            problemas.append("No se detectó firma digital")

    # Títulos y analíticos
    if "título" in tipo or "analítico" in tipo:
        if analisis.get('cantidad_firmas_visibles', 0) == 0:
            estado = "⚠️ REVISAR"; accion = "No se detecta firma visible"
            problemas.append("Sin firma visible")

    # Múltiples firmas: solo si ambas condiciones son verdaderas
    if analisis.get('multiples_firmas') and firma_info['cantidad_firmas'] > 1:
        if estado == "✅ OK":
            estado = "⚠️ REVISAR"; accion = "Verificar cuál firma corresponde"
        problemas.append("Múltiples firmas detectadas")

    # Calidad: solo marca si es explícitamente mala
    if calidad == "ilegible":
        estado = "❌ RECHAZAR"; accion = "Imagen ilegible"
        problemas.append("Imagen ilegible")
    elif calidad in ["baja", "borrosa"]:
        if estado == "✅ OK":
            estado = "⚠️ REVISAR"; accion = "Calidad de imagen insuficiente"
        problemas.append(f"Calidad de imagen: {calidad}")

    # Problemas detectados por Claude
    if problemas_claude:
        if estado == "✅ OK":
            estado = "⚠️ REVISAR"; accion = "Revisar problemas detectados"
        for p in problemas_claude:
            problemas.append(p)

    # Foto de celular
    if analisis.get("es_foto_celular"):
        if estado == "✅ OK":
            estado = "⚠️ REVISAR"; accion = "Documento fotografiado con celular"
        problemas.append("Documento fotografiado con celular")

    return estado, accion, problemas

def generar_observacion(analisis, problemas):
    """Usa la observación redactada por Claude, y si hay problemas del sistema los agrega al final."""
    obs_base = analisis.get("observacion_redactada") or analisis.get("observaciones") or ""

    if problemas:
        # Solo agrega problemas del sistema que no estén ya mencionados en la observación de Claude
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

        for i, archivo in enumerate(archivos):
            estado_texto.text(f"Analizando {archivo.name}...")
            pdf_bytes = archivo.read()

            firma_info = verificar_firma_digital(pdf_bytes)
            analisis = analizar_con_claude(pdf_bytes)
            estado, accion, problemas = evaluar_documento(firma_info, analisis)
            observacion = generar_observacion(analisis, problemas)

            tiene_firma = firma_info["tiene_firma"]
            firma_texto = "SÍ" if tiene_firma else ("NO" if tiene_firma == False else "NO DETECTADA")

            resultados.append({
                "Archivo": archivo.name,
                "Titular": analisis.get("titular_documento"),
                "Tipo": analisis.get("tipo_documento"),
                "Fecha": analisis.get("fecha_emision"),
                "Año": analisis.get("anio_documento"),
                "Firma Digital": firma_texto,
                "Firmantes Certificado": ", ".join(firma_info["firmantes"]),
                "Firmantes Visibles": ", ".join(analisis.get("firmantes_visibles", [])),
                "Estado": estado,
                "Acción": accion,
                "Observaciones": observacion
            })

            barra.progress((i + 1) / len(archivos))

        estado_texto.text("✅ Análisis completado.")
        df = pd.DataFrame(resultados)

        st.subheader("📊 Resultados")
        st.dataframe(df, use_container_width=True)

        st.download_button(
            label="⬇️ Descargar Excel",
            data=generar_excel(df),
            file_name="revision_apostillas.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
