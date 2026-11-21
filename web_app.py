"""
Aplicación Web pública / cloud para Hugging Face Spaces + Google Drive (+ Google Sites).
Soporta:
  - PUBLIC_MODE=1 (oculta edición de API key y bloquea POST /api/config con 403)
  - ACCESS_PIN opcional (protege consultas de chat/búsqueda si se define en Secrets)
  - Visor PDF vía Google Drive + caché en /tmp + fallback a URLs oficiales
  - Incrustable mediante iframe en Google Sites
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from drive_connector import fetch_pdf_bytes, get_drive_view_url, load_drive_map
from gemini_rag import (
    GeminiRAGEngine,
    auto_select_working_model,
    get_public_config,
    is_public_mode,
    list_gemini_models,
    load_gemini_config,
    save_gemini_config,
    test_gemini_connection,
)
from motor_consulta import MotorConsultaEstricto

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="gl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Antigravity ID Lexislación · Galicia (Gemini + Google Drive)</title>
  <style>
    :root {
      --bg: #f3f6fb;
      --surface: #ffffff;
      --surface-2: #f7faff;
      --border: #dce4f0;
      --primary: #0b4f8a;
      --primary-dark: #073863;
      --accent: #0077b6;
      --ok: #137333;
      --ok-bg: #e6f4ea;
      --warn: #b06000;
      --warn-bg: #fef7e0;
      --err: #b3261e;
      --err-bg: #fce8e6;
      --text: #182230;
      --muted: #5a6a80;
      --shadow: 0 8px 26px rgba(11, 79, 138, 0.08);
      --radius: 12px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.52;
    }
    header {
      background: linear-gradient(135deg, #073863 0%, #0b4f8a 60%, #0077b6 100%);
      color: #fff;
      padding: 18px 20px 14px;
      box-shadow: 0 4px 18px rgba(7, 56, 99, 0.2);
    }
    .wrap { max-width: 1380px; margin: 0 auto; padding: 0 16px; }
    .header-top { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 14px; }
    h1 { margin: 0; font-size: 1.42rem; font-weight: 700; letter-spacing: -0.01em; }
    .subtitle { margin: 3px 0 0; font-size: 0.86rem; opacity: 0.92; }
    .header-right { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
    .stat-pill {
      background: rgba(255,255,255,0.14);
      border: 1px solid rgba(255,255,255,0.24);
      padding: 5px 11px;
      border-radius: 999px;
      font-size: 0.79rem;
      font-weight: 600;
    }
    .gemini-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: rgba(255,255,255,0.18);
      border: 1px solid rgba(255,255,255,0.3);
      padding: 5px 12px;
      border-radius: 999px;
      font-size: 0.79rem;
      font-weight: 600;
    }
    .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
    .dot-ok { background: #6ee7b7; box-shadow: 0 0 0 3px rgba(110,231,183,0.25); }
    .dot-warn { background: #fcd34d; box-shadow: 0 0 0 3px rgba(252,211,77,0.25); }
    .btn-config {
      background: #fff;
      color: var(--primary-dark);
      border: none;
      padding: 7px 13px;
      border-radius: 8px;
      font-weight: 700;
      font-size: 0.82rem;
      cursor: pointer;
    }
    .btn-config:hover { background: #eaf2fb; }
    .tabs { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
    .tab-btn {
      background: rgba(255,255,255,0.15);
      color: #fff;
      border: 1px solid rgba(255,255,255,0.25);
      padding: 8px 15px;
      border-radius: 8px;
      cursor: pointer;
      font-weight: 600;
      font-size: 0.87rem;
    }
    .tab-btn.active { background: #fff; color: var(--primary-dark); }
    main { padding: 18px 0 42px; }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 18px;
      margin-bottom: 16px;
    }
    .hidden { display: none !important; }
    .chat-layout { display: grid; grid-template-columns: 1fr; gap: 16px; align-items: start; }
    .chat-layout.with-pdf { grid-template-columns: minmax(420px, 1.15fr) minmax(360px, 0.95fr); }
    @media (max-width: 1020px) { .chat-layout.with-pdf { grid-template-columns: 1fr; } }
    .chat-controls {
      display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
      justify-content: space-between; margin-bottom: 12px; padding-bottom: 12px;
      border-bottom: 1px solid var(--border);
    }
    .filters-inline { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font-size: 0.84rem; }
    input[type="text"], input[type="password"], select, textarea {
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 9px 12px;
      font-size: 0.92rem;
      background: #fff;
      color: var(--text);
      font-family: inherit;
    }
    .filters-inline select { padding: 6px 9px; font-size: 0.84rem; }
    .btn-primary {
      background: var(--primary); color: #fff; border: none; border-radius: 8px;
      padding: 10px 18px; font-size: 0.92rem; font-weight: 600; cursor: pointer;
    }
    .btn-primary:hover { background: var(--primary-dark); }
    .btn-primary:disabled { opacity: 0.55; cursor: not-allowed; }
    .btn-secondary {
      background: var(--surface-2); color: var(--primary-dark);
      border: 1px solid var(--border); border-radius: 8px;
      padding: 7px 12px; font-size: 0.84rem; font-weight: 600; cursor: pointer;
    }
    .btn-secondary:hover { background: #e8f1fb; }
    .examples { display: flex; flex-wrap: wrap; gap: 7px; margin-bottom: 12px; }
    .chip {
      background: var(--surface-2); border: 1px solid var(--border);
      border-radius: 999px; padding: 5px 11px; font-size: 0.79rem;
      color: var(--primary); cursor: pointer;
    }
    .chip:hover { background: #e6f0fc; border-color: var(--accent); }
    .chat-stream {
      min-height: 280px; max-height: 60vh; overflow-y: auto;
      padding: 6px 4px; display: flex; flex-direction: column; gap: 14px;
    }
    .empty-chat {
      background: var(--surface-2); border: 1px dashed var(--border);
      border-radius: 10px; padding: 20px; color: var(--muted); font-size: 0.91rem;
    }
    .msg-user {
      align-self: flex-end; max-width: 85%; background: var(--primary); color: #fff;
      padding: 11px 15px; border-radius: 14px 14px 3px 14px; font-size: 0.94rem;
    }
    .msg-assistant {
      align-self: stretch; background: #fff; border: 1px solid var(--border);
      border-left: 4px solid var(--primary); border-radius: 12px; padding: 15px 17px;
    }
    .msg-status { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 10px; }
    .badge { display: inline-block; padding: 3px 9px; border-radius: 999px; font-size: 0.74rem; font-weight: 700; }
    .badge-ok { background: var(--ok-bg); color: var(--ok); }
    .badge-warn { background: var(--warn-bg); color: var(--warn); }
    .badge-err { background: var(--err-bg); color: var(--err); }
    .badge-meta { background: #eef3f9; color: var(--primary-dark); font-weight: 600; }
    .md-body { font-size: 0.94rem; color: #15202e; }
    .md-body h2, .md-body h3, .md-body h4 { margin: 12px 0 6px; color: var(--primary-dark); }
    .md-body ul, .md-body ol { margin: 6px 0 10px 20px; padding: 0; }
    .md-body li { margin-bottom: 4px; }
    .md-body p { margin: 6px 0; }
    .cite-pill {
      display: inline-flex; align-items: center; gap: 4px;
      background: #e8f1fb; color: var(--primary-dark); border: 1px solid #bdd4f0;
      border-radius: 6px; padding: 1px 7px; margin: 0 2px;
      font-size: 0.78rem; font-weight: 700; cursor: pointer;
    }
    .cite-pill:hover { background: var(--primary); color: #fff; border-color: var(--primary); }
    .sources-box { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border); }
    .sources-title { font-size: 0.84rem; font-weight: 700; color: var(--primary-dark); margin-bottom: 8px; }
    .source-item {
      background: var(--surface-2); border: 1px solid #e2eaf5;
      border-radius: 9px; padding: 10px 12px; margin-bottom: 8px; font-size: 0.84rem;
    }
    .source-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: flex-start; gap: 8px; }
    .source-actions { display: flex; flex-wrap: wrap; gap: 6px; flex-shrink: 0; }
    .btn-mini {
      background: var(--primary); color: #fff; border: none; border-radius: 6px;
      padding: 5px 10px; font-size: 0.77rem; font-weight: 600; cursor: pointer; text-decoration: none;
    }
    .btn-mini:hover { background: var(--primary-dark); }
    .btn-mini.ghost { background: #fff; color: var(--primary-dark); border: 1px solid var(--border); }
    .btn-mini.ghost:hover { background: #e8f1fb; }
    .btn-mini.drive { background: #137333; color: #fff; }
    .btn-mini.drive:hover { background: #0d5425; }
    .source-extract {
      margin-top: 6px; color: #344256; font-size: 0.81rem; background: #fff;
      border-left: 3px solid #9dc0e6; padding: 6px 9px; border-radius: 4px;
    }
    .chat-input-form {
      display: flex; gap: 10px; margin-top: 14px; padding-top: 12px;
      border-top: 1px solid var(--border); align-items: flex-end;
    }
    .chat-input-form textarea { flex: 1; resize: vertical; min-height: 48px; max-height: 130px; }
    .pdf-panel {
      position: sticky; top: 14px; background: var(--surface);
      border: 1px solid var(--border); border-radius: var(--radius);
      box-shadow: var(--shadow); overflow: hidden;
    }
    .pdf-panel-head {
      background: #073863; color: #fff; padding: 11px 14px;
      display: flex; justify-content: space-between; align-items: center; gap: 10px;
    }
    .pdf-panel-title { font-size: 0.85rem; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .pdf-frame { width: 100%; height: 74vh; border: none; background: #525659; display: block; }
    .modal-backdrop {
      position: fixed; inset: 0; background: rgba(12, 26, 48, 0.56);
      display: flex; align-items: center; justify-content: center; z-index: 999; padding: 16px;
    }
    .modal {
      background: #fff; border-radius: 14px; max-width: 540px; width: 100%;
      padding: 22px; box-shadow: 0 20px 50px rgba(0,0,0,0.28);
    }
    .search-row { display: flex; gap: 10px; flex-wrap: wrap; }
    #queryInput, #docSearch { flex: 1; min-width: 240px; }
    .claim-card {
      border: 1px solid var(--border); border-left: 4px solid var(--primary);
      border-radius: 10px; padding: 14px; margin-top: 12px; background: #fff;
    }
    .claim-quote {
      font-size: 0.95rem; background: var(--surface-2); border-radius: 8px;
      padding: 10px 12px; border: 1px solid #e7edf6; margin: 8px 0;
    }
    .claim-meta { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font-size: 0.82rem; color: var(--muted); }
    table { width: 100%; border-collapse: collapse; font-size: 0.86rem; }
    th, td { text-align: left; padding: 9px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
    th { background: var(--surface-2); color: var(--primary-dark); font-weight: 700; }
  </style>
</head>
<body>
  <header>
    <div class="wrap">
      <div class="header-top">
        <div>
          <h1>Antigravity ID Lexislación · Galicia</h1>
          <p class="subtitle">Consultas con Gemini RAG Estricto sobre 3.734 PDFs oficiais (ESO, Bacharelato e FP) conectados con Google Drive.</p>
        </div>
        <div class="header-right">
          <div id="statsBar" style="display:flex; gap:8px; flex-wrap:wrap;">
            <span class="stat-pill">Cargando corpus...</span>
          </div>
          <span id="driveBadge" class="stat-pill">Drive: comprobando...</span>
          <span id="geminiBadge" class="gemini-pill"><span class="dot dot-warn"></span> Comprobando Gemini...</span>
          <button class="btn-config" onclick="autoProbeGeminiModel(true)">Auto-probar modelos</button>
          <button id="btnOpenConfig" class="btn-config hidden" onclick="openConfigModal()">Configurar Gemini API</button>
        </div>
      </div>
      <div class="tabs">
        <button class="tab-btn active" onclick="showTab('chat')">Chat con Gemini (RAG Estricto)</button>
        <button class="tab-btn" onclick="showTab('query')">Busca Literal Local (Sen API Key)</button>
        <button class="tab-btn" onclick="showTab('catalog')">Explorador do Corpus (PDFs)</button>
      </div>
    </div>
  </header>

  <main class="wrap">
    <div id="pinBar" class="card hidden" style="background:#fef7e0; border-color:#f6d478;">
      <form onsubmit="saveAccessPin(event)" style="display:flex; flex-wrap:wrap; gap:10px; align-items:center;">
        <strong>Acceso protexido con PIN:</strong>
        <input id="pinInput" type="password" placeholder="Introduce o PIN de acceso..." style="min-width:220px;" />
        <button type="submit" class="btn-primary">Desbloquear</button>
        <span id="pinStatus" style="font-size:0.84rem; color:var(--warn);"></span>
      </form>
    </div>

    <!-- TAB 1: CHAT GEMINI -->
    <section id="tabChat">
      <div id="chatLayout" class="chat-layout">
        <div class="card" style="margin-bottom:0;">
          <div class="chat-controls">
            <div class="filters-inline">
              <label>Etapa:
                <select id="chatEtapa">
                  <option value="">Todas (auto)</option>
                  <option value="ESO">ESO</option>
                  <option value="Bacharelato">Bacharelato</option>
                  <option value="FP">Formación Profesional (FP)</option>
                  <option value="Transversal">Transversal / Centros</option>
                </select>
              </label>
              <label>Ámbito:
                <select id="chatAmbito">
                  <option value="">Autonómico e Estatal</option>
                  <option value="Autonómico">Autonómico (Galicia)</option>
                  <option value="Estatal">Estatal (BOE)</option>
                </select>
              </label>
              <label>Páxinas contexto:
                <select id="chatMaxPages">
                  <option value="8" selected>8 páxinas</option>
                  <option value="12">12 páxinas</option>
                  <option value="16">16 páxinas</option>
                </select>
              </label>
            </div>
            <button class="btn-secondary" onclick="resetChat()">Nova conversa</button>
          </div>

          <div class="examples">
            <span class="chip" onclick="askExample(this)">¿Cuántas materias suspensas permiten titular en la ESO en Galicia y qué norma lo regula?</span>
            <span class="chip" onclick="askExample(this)">¿Cómo funciona la promoción y permanencia en Bachillerato en Galicia?</span>
            <span class="chip" onclick="askExample(this)">¿Qué regula el Decreto 20/2026 de Formación Profesional en Galicia?</span>
            <span class="chip" onclick="askExample(this)">¿Qué plazos y procedimiento existen para reclamar calificaciones?</span>
            <span class="chip" onclick="askExample(this)">¿Qué medidas de atención á diversidade existen para alumnado NEAE?</span>
            <span class="chip" onclick="askExample(this)">¿Qué indican las instrucciones de inicio de curso 2026/27 sobre Proens y XADE?</span>
          </div>

          <div id="modelProbeBanner" class="hidden" style="margin-bottom:10px; padding:9px 12px; border-radius:8px; border:1px solid var(--border); background:var(--surface-2); font-size:0.82rem; color:var(--primary-dark);"></div>

          <div id="chatStream" class="chat-stream">
            <div class="empty-chat" id="emptyChatMsg">
              <strong>Listo para consultar a lexislación educativa vixente de Galicia.</strong><br/>
              Escribe calquera dúbida sobre ESO, Bacharelato, FP, atención á diversidade, avaliación ou resolucións recentes.<br/>
              O motor busca primeiro nos 3.734 PDFs indexados, obriga a Gemini a responder só co texto literal recuperado e indica o arquivo exacto e a páxina para abrilo no visor lateral ou en Google Drive.
            </div>
          </div>

          <form class="chat-input-form" onsubmit="sendChat(event)">
            <textarea id="chatInput" placeholder="Escribe a túa pregunta (Enter para enviar, Shift+Enter para salto de liña)..." onkeydown="handleChatKey(event)"></textarea>
            <button id="chatSendBtn" type="submit" class="btn-primary">Preguntar a Gemini</button>
          </form>
        </div>

        <aside id="pdfPanel" class="pdf-panel hidden">
          <div class="pdf-panel-head">
            <div id="pdfPanelTitle" class="pdf-panel-title">Visor PDF</div>
            <div style="display:flex; gap:6px; align-items:center;">
              <a id="pdfDriveLink" class="btn-mini drive hidden" href="#" target="_blank" rel="noopener">Google Drive</a>
              <a id="pdfExternalLink" class="btn-mini ghost" href="#" target="_blank" rel="noopener">Abrir fóra</a>
              <button class="btn-mini" style="background:#b3261e;" onclick="closePdfViewer()">Pechar</button>
            </div>
          </div>
          <iframe id="pdfFrame" class="pdf-frame" src="about:blank" title="Visor PDF oficial"></iframe>
        </aside>
      </div>
    </section>

    <!-- TAB 2: BUSCA LITERAL LOCAL -->
    <section id="tabQuery" class="hidden">
      <div class="card">
        <form onsubmit="runQuery(event)">
          <div class="search-row">
            <input id="queryInput" type="text" placeholder="Busca literal directa nos PDFs (non require API Key)..." />
            <button type="submit" class="btn-primary">Buscar no Corpus</button>
          </div>
          <div style="display:flex; flex-wrap:wrap; gap:10px; margin-top:10px; font-size:0.85rem;">
            <label>Etapa:
              <select id="filterEtapa">
                <option value="">Todas as etapas</option>
                <option value="ESO">ESO</option>
                <option value="Bacharelato">Bacharelato</option>
                <option value="FP">Formación Profesional (FP)</option>
                <option value="Transversal">Transversal / Centros</option>
              </select>
            </label>
            <label>Ámbito:
              <select id="filterAmbito">
                <option value="">Autonómico e Estatal</option>
                <option value="Autonómico">Autonómico (Galicia)</option>
                <option value="Estatal">Estatal (BOE)</option>
              </select>
            </label>
            <label>Tipo:
              <select id="filterTipo">
                <option value="">Todos os tipos</option>
                <option value="Decreto">Decreto</option>
                <option value="Orde">Orde</option>
                <option value="Resolución">Resolución</option>
                <option value="Instrución">Instrución</option>
                <option value="Real Decreto">Real Decreto</option>
                <option value="Lei Orgánica">Lei Orgánica</option>
              </select>
            </label>
            <label>Citas máx.:
              <select id="filterTop">
                <option value="6">6 evidencias</option>
                <option value="10">10 evidencias</option>
                <option value="15">15 evidencias</option>
              </select>
            </label>
          </div>
        </form>
      </div>
      <div id="resultBox" class="card hidden"></div>
    </section>

    <!-- TAB 3: EXPLORADOR DO CORPUS -->
    <section id="tabCatalog" class="hidden">
      <div class="card">
        <div class="search-row">
          <input id="docSearch" type="text" placeholder="Filtrar documentos por título, arquivo ou boletín..." oninput="loadCatalog(0)" />
          <select id="docFolder" onchange="loadCatalog(0)">
            <option value="">Todos os cartafoles</option>
            <option value="03_Autonomica_Consolidada_LexGal">03 · Autonómica Consolidada (Lex.gal)</option>
            <option value="04_ESO">04 · ESO</option>
            <option value="05_Bacharelato">05 · Bacharelato</option>
            <option value="06_Formacion_Profesional">06 · Formación Profesional</option>
            <option value="07_Transversal_Centros_Diversidade">07 · Transversal / Centros / Diversidade</option>
            <option value="08_Resolucions_Instrucions_Anuncios">08 · Resolucións, Instrucións e Anuncios</option>
            <option value="02_Estatal_BOE_Normas">02 · Estatal (BOE Normas)</option>
            <option value="01_Estatal_BOE_Codigos">01 · Estatal (BOE Códigos)</option>
          </select>
        </div>
        <div id="catalogSummary" style="margin:10px 0; font-size:0.87rem; color:var(--muted);"></div>
        <div style="overflow-x:auto;">
          <table>
            <thead>
              <tr>
                <th>Título oficial</th>
                <th>Ámbito / Etapas</th>
                <th>Tipo / Data</th>
                <th>Páxinas</th>
                <th>PDF</th>
              </tr>
            </thead>
            <tbody id="catalogBody"></tbody>
          </table>
        </div>
      </div>
    </section>
  </main>

  <!-- MODAL CONFIGURACIÓN GEMINI (solo modo local) -->
  <div id="configModal" class="modal-backdrop hidden">
    <div class="modal">
      <h2 style="margin:0 0 8px; color:var(--primary-dark);">Configurar conexión con Gemini API</h2>
      <div style="margin:12px 0;">
        <label for="cfgApiKey" style="display:block; font-size:0.85rem; font-weight:700;">Gemini API Key <span id="cfgKeyHint" style="font-weight:400; color:var(--muted);"></span></label>
        <div style="display:flex; gap:8px; margin-top:5px;">
          <input id="cfgApiKey" type="password" placeholder="Pega a túa API Key..." style="flex:1;" />
          <button type="button" class="btn-secondary" onclick="toggleKeyVisibility()">Ver</button>
        </div>
      </div>
      <div style="margin:12px 0;">
        <label for="cfgModel" style="display:block; font-size:0.85rem; font-weight:700;">Modelo Gemini</label>
        <div style="display:flex; gap:8px; margin-top:5px;">
          <select id="cfgModel" style="flex:1;">
            <option value="gemini-3.5-flash-lite">gemini-3.5-flash-lite</option>
          </select>
          <button type="button" class="btn-secondary" id="testConnBtn" onclick="testAndLoadModels()">Probar modelos</button>
        </div>
      </div>
      <div id="cfgStatusMsg" style="font-size:0.85rem; margin:10px 0; min-height:20px;"></div>
      <div style="display:flex; justify-content:flex-end; gap:8px; margin-top:14px;">
        <button type="button" class="btn-secondary" onclick="closeConfigModal()">Cancelar</button>
        <button type="button" class="btn-primary" onclick="saveConfig()">Gardar</button>
      </div>
    </div>
  </div>

  <script>
    let chatHistory = [];
    let currentEvidenceStore = {};
    let msgSeq = 0;
    let isPublicMode = false;
    let accessPin = sessionStorage.getItem('lex_pin') || '';

    function esc(s) {
      return String(s || '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function apiHeaders() {
      const h = {'Content-Type': 'application/json'};
      if (accessPin) h['X-Access-Pin'] = accessPin;
      return h;
    }

    function saveAccessPin(ev) {
      ev.preventDefault();
      accessPin = document.getElementById('pinInput').value.trim();
      sessionStorage.setItem('lex_pin', accessPin);
      document.getElementById('pinStatus').textContent = 'PIN gardado para esta sesión.';
      document.getElementById('pinBar').classList.add('hidden');
      loadConfigStatus(true);
    }

    function showTab(tab) {
      document.getElementById('tabChat').classList.toggle('hidden', tab !== 'chat');
      document.getElementById('tabQuery').classList.toggle('hidden', tab !== 'query');
      document.getElementById('tabCatalog').classList.toggle('hidden', tab !== 'catalog');
      const btns = document.querySelectorAll('.tab-btn');
      btns[0].classList.toggle('active', tab === 'chat');
      btns[1].classList.toggle('active', tab === 'query');
      btns[2].classList.toggle('active', tab === 'catalog');
      if (tab === 'catalog') loadCatalog(0);
    }

    function openPdfInPanel(relPath, pageNum, label, driveUrl) {
      const panel = document.getElementById('pdfPanel');
      const layout = document.getElementById('chatLayout');
      const frame = document.getElementById('pdfFrame');
      const titleEl = document.getElementById('pdfPanelTitle');
      const extLink = document.getElementById('pdfExternalLink');
      const drvLink = document.getElementById('pdfDriveLink');
      const encRel = encodeURI(relPath);
      const bust = Date.now();
      const targetUrl = `/pdf/${encRel}?v=${bust}#page=${pageNum || 1}`;
      const cleanUrl = `/pdf/${encRel}#page=${pageNum || 1}`;
      titleEl.textContent = `${label || relPath} · Páx. ${pageNum || 1}`;
      extLink.href = cleanUrl;
      if (driveUrl) {
        drvLink.href = driveUrl;
        drvLink.classList.remove('hidden');
      } else {
        drvLink.classList.add('hidden');
      }
      frame.src = targetUrl;
      panel.classList.remove('hidden');
      layout.classList.add('with-pdf');
      showTab('chat');
    }

    function closePdfViewer() {
      document.getElementById('pdfPanel').classList.add('hidden');
      document.getElementById('chatLayout').classList.remove('with-pdf');
      document.getElementById('pdfFrame').src = 'about:blank';
    }

    function openCitedEvidence(msgId, citeId) {
      const ev = (currentEvidenceStore[msgId] || {})[citeId];
      if (!ev) return;
      openPdfInPanel(ev.ruta_relativa, ev.pagina_pdf, ev.documento, ev.drive_url || '');
    }

    function renderMarkdownSafe(mdText, msgId, evMap) {
      let safe = esc(mdText || '');
      safe = safe.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      safe = safe.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
      safe = safe.replace(/`([^`\n]+)`/g, '<code>$1</code>');
      safe = safe.replace(/\[\[(E\d+)\]\]/g, (_, cid) => {
        const ev = evMap[cid];
        if (!ev) return '';
        const shortName = ev.documento.length > 34 ? ev.documento.slice(0, 31) + '...' : ev.documento;
        return `<button type="button" class="cite-pill" title="${esc(ev.titulo)} (Páx. PDF ${ev.pagina_pdf})" onclick="openCitedEvidence('${msgId}','${cid}')">${cid} · ${esc(shortName)} · p.${ev.pagina_pdf}</button>`;
      });

      const lines = safe.split(/\r?\n/);
      let out = [], inUl = false, inOl = false;
      const closeLists = () => {
        if (inUl) { out.push('</ul>'); inUl = false; }
        if (inOl) { out.push('</ol>'); inOl = false; }
      };
      for (const rawLine of lines) {
        const ln = rawLine.trim();
        if (!ln) { closeLists(); continue; }
        if (ln.startsWith('### ')) { closeLists(); out.push(`<h4>${ln.slice(4)}</h4>`); continue; }
        if (ln.startsWith('## ')) { closeLists(); out.push(`<h3>${ln.slice(3)}</h3>`); continue; }
        if (ln.startsWith('# ')) { closeLists(); out.push(`<h2>${ln.slice(2)}</h2>`); continue; }
        if (/^[-*•]\s+/.test(ln)) {
          if (inOl) { out.push('</ol>'); inOl = false; }
          if (!inUl) { out.push('<ul>'); inUl = true; }
          out.push(`<li>${ln.replace(/^[-*•]\s+/, '')}</li>`);
          continue;
        }
        if (/^\d+\.\s+/.test(ln)) {
          if (inUl) { out.push('</ul>'); inUl = false; }
          if (!inOl) { out.push('<ol>'); inOl = true; }
          out.push(`<li>${ln.replace(/^\d+\.\s+/, '')}</li>`);
          continue;
        }
        closeLists();
        out.push(`<p>${ln}</p>`);
      }
      closeLists();
      return `<div class="md-body">${out.join('')}</div>`;
    }

    function appendUserBubble(text) {
      const empty = document.getElementById('emptyChatMsg');
      if (empty) empty.remove();
      const stream = document.getElementById('chatStream');
      const div = document.createElement('div');
      div.className = 'msg-user';
      div.textContent = text;
      stream.appendChild(div);
      stream.scrollTop = stream.scrollHeight;
    }

    function appendLoadingBubble() {
      const stream = document.getElementById('chatStream');
      const div = document.createElement('div');
      div.className = 'msg-assistant';
      div.innerHTML = '<em>Buscando páxinas exactas nos 3.734 PDFs e redactando resposta verificada con Gemini...</em>';
      stream.appendChild(div);
      stream.scrollTop = stream.scrollHeight;
      return div;
    }

    function renderAssistantBubble(res) {
      const msgId = 'm_' + (++msgSeq);
      const stream = document.getElementById('chatStream');
      const div = document.createElement('div');
      div.className = 'msg-assistant';

      const allEvs = [...(res.fuentes_citadas || []), ...(res.evidencias_consultadas || [])];
      const evMap = {};
      for (const e of allEvs) { if (!evMap[e.cite_id]) evMap[e.cite_id] = e; }
      currentEvidenceStore[msgId] = evMap;

      const statusBadge = res.encontrado
        ? `<span class="badge badge-ok">Evidencia Verificada · ${esc(res.modelo_usado || 'Gemini')}</span>`
        : `<span class="badge badge-warn">Abstención Estrita · Sen soporte no corpus</span>`;

      const sourcesToShow = (res.fuentes_citadas && res.fuentes_citadas.length)
        ? res.fuentes_citadas
        : (res.evidencias_consultadas || []);
      const sourcesTitle = (res.fuentes_citadas && res.fuentes_citadas.length)
        ? `Arquivos e páxinas exactas citadas (${res.fuentes_citadas.length})`
        : `Páxinas recuperadas do corpus (${sourcesToShow.length})`;

      let sourcesHtml = '';
      if (sourcesToShow.length) {
        sourcesHtml = `<div class="sources-box"><div class="sources-title">${sourcesTitle}</div>`;
        for (const s of sourcesToShow) {
          const bol = s.pagina_boletin ? `<span class="badge badge-meta">${esc(s.pagina_boletin)}</span>` : '';
          const ref = s.referencia_interna ? `<span class="badge badge-meta">${esc(s.referencia_interna)}</span>` : '';
          const drvBtn = s.drive_url
            ? `<a class="btn-mini drive" href="${esc(s.drive_url)}" target="_blank" rel="noopener">Drive</a>`
            : '';
          sourcesHtml += `
            <div class="source-item">
              <div class="source-head">
                <div>
                  <span class="badge badge-meta">${esc(s.cite_id)}</span>
                  <strong>${esc(s.titulo)}</strong><br/>
                  <span style="color:var(--muted);">
                    Arquivo: <code>${esc(s.documento)}</code> ·
                    <strong>Páx. PDF ${s.pagina_pdf}/${s.page_count}</strong> ${bol} ${ref}
                  </span>
                </div>
                <div class="source-actions">
                  <button type="button" class="btn-mini" onclick="openPdfInPanel('${esc(s.ruta_relativa)}', ${s.pagina_pdf}, '${esc(s.documento)}', '${esc(s.drive_url || '')}')">Ver aquí (p.${s.pagina_pdf})</button>
                  ${drvBtn}
                  <a class="btn-mini ghost" href="${esc(s.pdf_url_local)}" target="_blank" rel="noopener">Pestana</a>
                </div>
              </div>
              <div class="source-extract">“${esc(s.extracto_literal)}”</div>
            </div>
          `;
        }
        sourcesHtml += '</div>';
      }

      const searchesHtml = (res.busquedas_ejecutadas && res.busquedas_ejecutadas.length)
        ? `<details style="margin-top:8px; font-size:0.8rem; color:var(--muted);"><summary>Búsquedas executadas no corpus (${res.busquedas_ejecutadas.length})</summary><ul style="margin:4px 0 0 18px;">${res.busquedas_ejecutadas.map(q => `<li><code>${esc(q)}</code></li>`).join('')}</ul></details>`
        : '';

      div.innerHTML = `
        <div class="msg-status">
          ${statusBadge}
          ${res.etapa_aplicada ? `<span class="badge badge-meta">Etapa: ${esc(res.etapa_aplicada)}</span>` : ''}
        </div>
        ${renderMarkdownSafe(res.respuesta_markdown || '', msgId, evMap)}
        ${sourcesHtml}
        ${searchesHtml}
      `;
      stream.appendChild(div);
      stream.scrollTop = stream.scrollHeight;
    }

    function appendErrorBubble(errMsg, needsConfig, needsPin) {
      const stream = document.getElementById('chatStream');
      const div = document.createElement('div');
      div.className = 'msg-assistant';
      div.innerHTML = `<div class="msg-status"><span class="badge badge-err">Aviso</span></div><p>${esc(errMsg)}</p>`;
      if (needsPin) document.getElementById('pinBar').classList.remove('hidden');
      if (needsConfig && !isPublicMode) openConfigModal();
      stream.appendChild(div);
      stream.scrollTop = stream.scrollHeight;
    }

    function handleChatKey(ev) {
      if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); sendChat(); }
    }
    function askExample(el) { document.getElementById('chatInput').value = el.textContent; sendChat(); }
    function resetChat() {
      chatHistory = []; currentEvidenceStore = {};
      document.getElementById('chatStream').innerHTML = `<div class="empty-chat" id="emptyChatMsg"><strong>Nova conversa iniciada.</strong></div>`;
    }

    async function sendChat(ev) {
      if (ev) ev.preventDefault();
      const inp = document.getElementById('chatInput');
      const btn = document.getElementById('chatSendBtn');
      const q = inp.value.trim();
      if (!q) return;

      inp.value = ''; inp.disabled = true; btn.disabled = true;
      appendUserBubble(q);
      const loader = appendLoadingBubble();
      try {
        const r = await fetch('/api/chat', {
          method: 'POST',
          headers: apiHeaders(),
          body: JSON.stringify({
            query: q,
            history: chatHistory.slice(-6),
            etapa: document.getElementById('chatEtapa').value,
            ambito: document.getElementById('chatAmbito').value,
            max_pages: Number(document.getElementById('chatMaxPages').value || 8)
          })
        });
        const data = await r.json();
        loader.remove();
        if (!r.ok) {
          appendErrorBubble(data.error || 'Erro chamando á API', Boolean(data.needs_config), Boolean(data.needs_pin));
          return;
        }
        renderAssistantBubble(data);
        if (data.modelo_usado) {
          document.getElementById('geminiBadge').innerHTML = `<span class="dot dot-ok"></span> Gemini activo: ${esc(data.modelo_usado)}`;
        }
        chatHistory.push({role: 'user', content: q});
        chatHistory.push({role: 'assistant', content: (data.respuesta_markdown || '').slice(0, 700)});
      } catch (e) {
        loader.remove();
        appendErrorBubble('Erro de rede: ' + e.message, false, false);
      } finally {
        inp.disabled = false; btn.disabled = false; inp.focus();
      }
    }

    async function loadConfigStatus(runAutoProbe = false) {
      const r = await fetch('/api/config');
      const cfg = await r.json();
      isPublicMode = Boolean(cfg.public_mode);
      document.getElementById('btnOpenConfig').classList.toggle('hidden', isPublicMode);
      if (cfg.pin_required && !accessPin) {
        document.getElementById('pinBar').classList.remove('hidden');
      }

      const badge = document.getElementById('geminiBadge');
      if (cfg.configured) {
        badge.innerHTML = `<span class="dot dot-ok"></span> Gemini activo: ${esc(cfg.model)}`;
        document.getElementById('cfgKeyHint').textContent = `(Gardada: ${cfg.masked_key})`;
      } else {
        badge.innerHTML = `<span class="dot dot-warn"></span> Gemini sen configurar`;
      }
      if (runAutoProbe && cfg.configured && (!cfg.pin_required || accessPin)) {
        await autoProbeGeminiModel(false);
      }
    }

    async function autoProbeGeminiModel(forceFromModal = false) {
      const badge = document.getElementById('geminiBadge');
      const banner = document.getElementById('modelProbeBanner');
      badge.innerHTML = `<span class="dot dot-warn"></span> Probando modelos Gemini...`;
      banner.classList.remove('hidden');
      banner.innerHTML = 'Probando modelos dispoñibles na conta Gemini para seleccionar o mellor operativo...';
      try {
        const keyInput = document.getElementById('cfgApiKey');
        const r = await fetch('/api/gemini/auto-select', {
          method: 'POST',
          headers: apiHeaders(),
          body: JSON.stringify({
            api_key: forceFromModal && keyInput && !isPublicMode ? keyInput.value.trim() : '',
            preferred_model: document.getElementById('cfgModel').value,
            save: !isPublicMode
          })
        });
        const d = await r.json();
        if (!r.ok) {
          if (d.needs_pin) document.getElementById('pinBar').classList.remove('hidden');
          badge.innerHTML = `<span class="dot dot-warn"></span> Gemini sen modelo activo`;
          banner.innerHTML = `<strong style="color:var(--err);">Aviso na autodetección:</strong> ${esc(d.error || 'Non se puido validar')}`;
          return;
        }
        badge.innerHTML = `<span class="dot dot-ok"></span> Gemini activo: ${esc(d.selected_model)}`;
        const probesHtml = (d.probe_results || []).map(p => {
          const color = p.ok ? 'var(--ok)' : 'var(--muted)';
          const tag = p.ok ? `200 OK (${p.latency_ms}ms)` : `HTTP ${p.status}`;
          return `<span style="color:${color}; margin-right:8px;"><code>${esc(p.id)}</code>: ${tag}</span>`;
        }).join('');
        banner.innerHTML = `
          <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
            <span><strong style="color:var(--ok);">Conectado con <code>${esc(d.selected_model)}</code></strong> · Autoselección completada</span>
            <button type="button" class="btn-mini ghost" onclick="document.getElementById('modelProbeBanner').classList.add('hidden')">Ocultar</button>
          </div>
          <div style="margin-top:4px; font-size:0.78rem;">Probas realizadas: ${probesHtml}</div>
        `;
      } catch (e) {
        badge.innerHTML = `<span class="dot dot-warn"></span> Erro probando Gemini`;
        banner.innerHTML = `<span style="color:var(--err);">Erro de rede: ${esc(e.message)}</span>`;
      }
    }

    function openConfigModal() {
      if (isPublicMode) return;
      document.getElementById('configModal').classList.remove('hidden');
    }
    function closeConfigModal() { document.getElementById('configModal').classList.add('hidden'); }
    function toggleKeyVisibility() {
      const inp = document.getElementById('cfgApiKey');
      inp.type = inp.type === 'password' ? 'text' : 'password';
    }
    async function testAndLoadModels() {
      const msg = document.getElementById('cfgStatusMsg');
      msg.textContent = 'Probando conexión...';
      const r = await fetch('/api/gemini/test', {
        method: 'POST', headers: apiHeaders(),
        body: JSON.stringify({api_key: document.getElementById('cfgApiKey').value.trim(), model: document.getElementById('cfgModel').value})
      });
      const d = await r.json();
      msg.textContent = r.ok ? d.message : (d.error || 'Erro');
      if (r.ok) {
        const sel = document.getElementById('cfgModel');
        sel.innerHTML = d.models.map(m => `<option value="${esc(m.id)}">${esc(m.id)}</option>`).join('');
        sel.value = d.selected_model;
      }
    }
    async function saveConfig() {
      const r = await fetch('/api/config', {
        method: 'POST', headers: apiHeaders(),
        body: JSON.stringify({api_key: document.getElementById('cfgApiKey').value.trim() || null, model: document.getElementById('cfgModel').value})
      });
      if (r.ok) { closeConfigModal(); loadConfigStatus(true); }
    }

    async function runQuery(ev) {
      if (ev) ev.preventDefault();
      const q = document.getElementById('queryInput').value.trim();
      if (!q) return;
      const box = document.getElementById('resultBox');
      box.classList.remove('hidden');
      box.innerHTML = '<p>Buscando e verificando citas exactas...</p>';
      const r = await fetch('/api/query', {
        method: 'POST', headers: apiHeaders(),
        body: JSON.stringify({
          query: q,
          etapa: document.getElementById('filterEtapa').value,
          ambito: document.getElementById('filterAmbito').value,
          doc_type: document.getElementById('filterTipo').value,
          top_k: Number(document.getElementById('filterTop').value || 6)
        })
      });
      const res = await r.json();
      if (!r.ok) { box.innerHTML = `<p style="color:var(--err);">${esc(res.error || 'Erro')}</p>`; return; }
      if (!res.encontrado) {
        box.innerHTML = `<div><span class="badge badge-warn">Abstención Estrita</span></div><p style="margin-top:10px;">${esc(res.respuesta_texto)}</p>`;
        return;
      }
      let html = `<div><span class="badge badge-ok">Evidencia Verificada · ${res.total_afirmaciones} citas exactas</span></div>`;
      for (const c of res.afirmaciones) {
        const pdfHref = '/pdf/' + encodeURI(c.ruta_relativa) + '#page=' + c.pagina_pdf;
        const drvBtn = c.drive_url ? `<a class="btn-mini drive" href="${esc(c.drive_url)}" target="_blank" rel="noopener">Drive</a>` : '';
        html += `
          <div class="claim-card">
            <div style="font-weight:700; color:var(--primary-dark);">${esc(c.titulo)}</div>
            <div class="claim-quote">“${esc(c.extracto_literal)}”</div>
            <div class="claim-meta">
              <span class="badge badge-meta">Páx. PDF ${c.pagina_pdf}/${c.page_count}</span>
              ${c.pagina_boletin ? `<span class="badge badge-meta">${esc(c.pagina_boletin)}</span>` : ''}
              <span>Arquivo: <code>${esc(c.documento)}</code></span>
              <button type="button" class="btn-mini" style="margin-left:auto;" onclick="openPdfInPanel('${esc(c.ruta_relativa)}', ${c.pagina_pdf}, '${esc(c.documento)}', '${esc(c.drive_url || '')}')">Ver aquí (p.${c.pagina_pdf})</button>
              ${drvBtn}
              <a class="btn-mini ghost" href="${pdfHref}" target="_blank" rel="noopener">Pestana</a>
            </div>
          </div>
        `;
      }
      box.innerHTML = html;
    }

    async function loadCatalog(offset) {
      const q = document.getElementById('docSearch').value.trim();
      const folder = document.getElementById('docFolder').value;
      const params = new URLSearchParams({search: q, folder: folder, limit: '80', offset: String(offset || 0)});
      const r = await fetch('/api/docs?' + params.toString(), {headers: apiHeaders()});
      const d = await r.json();
      if (!r.ok) { document.getElementById('catalogSummary').textContent = d.error || 'Erro'; return; }
      document.getElementById('catalogSummary').textContent = `Amosando ${d.items.length} de ${d.total} documentos oficiais indexados.`;
      document.getElementById('catalogBody').innerHTML = d.items.map(item => `
        <tr>
          <td><strong>${esc(item.title)}</strong><br><small style="color:var(--muted)">${esc(item.file_name)} · ${esc(item.boletin)}</small></td>
          <td>${esc(item.ambito)}<br><small>${esc((item.etapas || []).join(', '))}</small></td>
          <td>${esc(item.doc_type)}<br><small>${esc(item.pub_date || '-')}</small></td>
          <td>${item.page_count} páx.</td>
          <td style="white-space:nowrap;">
            <button type="button" class="btn-mini" onclick="openPdfInPanel('${esc(item.relative_path)}', 1, '${esc(item.file_name)}', '${esc(item.drive_url || '')}')">Visor</button>
            ${item.drive_url ? `<a class="btn-mini drive" href="${esc(item.drive_url)}" target="_blank" rel="noopener">Drive</a>` : ''}
            <a class="btn-mini ghost" href="/pdf/${encodeURI(item.relative_path)}#page=1" target="_blank" rel="noopener">Pestana</a>
          </td>
        </tr>
      `).join('');
    }

    async function loadStats() {
      const r = await fetch('/api/stats');
      const d = await r.json();
      document.getElementById('statsBar').innerHTML = `
        <span class="stat-pill">${d.documents.toLocaleString('es-ES')} PDFs</span>
        <span class="stat-pill">${d.pages.toLocaleString('es-ES')} páxinas reais</span>
      `;
      const drv = document.getElementById('driveBadge');
      if (d.drive_mapped_files > 0) {
        drv.textContent = `Google Drive: ${d.drive_mapped_files} PDFs`;
      } else {
        drv.textContent = `Drive pendente · Respaldo oficial activo`;
      }
    }

    loadStats();
    loadConfigStatus(true);
  </script>
</body>
</html>
"""


def make_handler(motor: MotorConsultaEstricto, rag_engine: GeminiRAGEngine):
    access_pin = os.environ.get("ACCESS_PIN", "").strip()

    class RequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict[str, Any] | None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(min(length, 131072))
            try:
                data = json.loads(raw.decode("utf-8"))
                return data if isinstance(data, dict) else None
            except Exception:
                return None

        def _check_pin(self) -> bool:
            if not access_pin:
                return True
            provided = (self.headers.get("X-Access-Pin") or "").strip()
            if provided != access_pin:
                self._send_json({"error": "PIN de acceso incorrecto ou ausente.", "needs_pin": True}, status=401)
                return False
            return True

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/" or path == "/index.html":
                body = HTML_PAGE.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/stats":
                stats = motor.get_stats()
                dmap = load_drive_map()
                stats["drive_mapped_files"] = dmap.get("total_files", 0)
                stats["public_mode"] = is_public_mode()
                self._send_json(stats)
                return

            if path == "/api/config":
                cfg = get_public_config()
                cfg["pin_required"] = bool(access_pin)
                self._send_json(cfg)
                return

            if path == "/api/docs":
                if not self._check_pin():
                    return
                qs = parse_qs(parsed.query)
                res = motor.list_documents(
                    search=qs.get("search", [""])[0],
                    etapa=qs.get("etapa", [""])[0],
                    ambito=qs.get("ambito", [""])[0],
                    doc_type=qs.get("doc_type", [""])[0],
                    folder=qs.get("folder", [""])[0],
                    limit=min(int(qs.get("limit", ["80"])[0]), 250),
                    offset=max(int(qs.get("offset", ["0"])[0]), 0),
                )
                for item in res["items"]:
                    item["drive_url"] = get_drive_view_url(item["relative_path"], item["file_name"])
                self._send_json(res)
                return

            if path.startswith("/pdf/"):
                rel = unquote(path[len("/pdf/"):]).lstrip("/")
                doc_info = motor.get_document_info(rel)
                if not doc_info:
                    self.send_error(HTTPStatus.NOT_FOUND, "Documento non atopado no índice")
                    return
                try:
                    data, source_tag = fetch_pdf_bytes(
                        relative_path=doc_info["relative_path"],
                        file_name=doc_info["file_name"],
                        official_pdf_url=doc_info["pdf_url"],
                    )
                except FileNotFoundError as exc:
                    self.send_error(HTTPStatus.NOT_FOUND, str(exc))
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", f'inline; filename="{doc_info["file_name"]}"')
                self.send_header("X-PDF-Source", source_tag)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            self.send_error(HTTPStatus.NOT_FOUND, "Ruta non atopada")

        def do_POST(self) -> None:
            parsed = urlparse(self.path)

            if parsed.path == "/api/config":
                if is_public_mode():
                    self._send_json({"error": "Configuración bloqueada en modo público."}, status=403)
                    return
                body = self._read_json_body()
                if body is None:
                    self._send_json({"error": "JSON inválido"}, status=400)
                    return
                api_key = body.get("api_key")
                model = body.get("model")
                cfg = save_gemini_config(
                    api_key=str(api_key) if api_key is not None else None,
                    model=str(model) if model is not None else None,
                )
                self._send_json(cfg)
                return

            if not self._check_pin():
                return

            body = self._read_json_body()
            if body is None:
                self._send_json({"error": "JSON inválido"}, status=400)
                return

            if parsed.path == "/api/query":
                res = motor.consultar(
                    query=str(body.get("query", "")),
                    etapa=str(body.get("etapa", "")),
                    ambito=str(body.get("ambito", "")),
                    doc_type=str(body.get("doc_type", "")),
                    top_k=min(max(int(body.get("top_k", 6) or 6), 1), 20),
                )
                for cl in res.get("afirmaciones", []):
                    cl["drive_url"] = get_drive_view_url(cl["ruta_relativa"], cl["documento"])
                self._send_json(res)
                return

            if parsed.path == "/api/gemini/models":
                try:
                    raw_key = "" if is_public_mode() else str(body.get("api_key") or "").strip()
                    eff_key = raw_key or load_gemini_config()["api_key"]
                    models = list_gemini_models(eff_key)
                    self._send_json({"models": models})
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=502)
                return

            if parsed.path == "/api/gemini/test":
                try:
                    raw_key = "" if is_public_mode() else str(body.get("api_key") or "")
                    res = test_gemini_connection(api_key=raw_key, model=str(body.get("model") or ""))
                    self._send_json(res)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=502)
                return

            if parsed.path == "/api/gemini/auto-select":
                try:
                    raw_key = "" if is_public_mode() else str(body.get("api_key") or "")
                    res = auto_select_working_model(
                        api_key=raw_key,
                        preferred_model=str(body.get("preferred_model") or ""),
                        save=bool(body.get("save", True)) and not is_public_mode(),
                    )
                    self._send_json(res)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=502)
                return

            if parsed.path == "/api/chat":
                try:
                    res = rag_engine.preguntar(
                        query=str(body.get("query", "")),
                        history=body.get("history") if isinstance(body.get("history"), list) else [],
                        etapa=str(body.get("etapa", "")),
                        ambito=str(body.get("ambito", "")),
                        doc_type=str(body.get("doc_type", "")),
                        max_pages=min(max(int(body.get("max_pages", 8) or 8), 4), 18),
                        model=str(body.get("model", "")),
                    )
                    self._send_json(res)
                except PermissionError as exc:
                    self._send_json({"error": str(exc), "needs_config": True}, status=400)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=502)
                return

            self.send_error(HTTPStatus.NOT_FOUND, "Ruta non atopada")

    return RequestHandler


def create_server(host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    eff_host = host if host is not None else os.environ.get("HOST", "0.0.0.0")
    eff_port = port if port is not None else int(os.environ.get("PORT", "7860"))
    motor = MotorConsultaEstricto()
    rag_engine = GeminiRAGEngine()
    return ThreadingHTTPServer((eff_host, eff_port), make_handler(motor, rag_engine))


def main() -> None:
    parser = argparse.ArgumentParser(description="Servidor Web Cloud Lexislación Galicia")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "7860")))
    args = parser.parse_args()
    srv = create_server(args.host, args.port)
    print(f"Servidor Web Cloud activo en http://{args.host}:{args.port} (PUBLIC_MODE={is_public_mode()})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
