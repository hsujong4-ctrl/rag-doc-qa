# RAG 文件問答（Gradio + FastAPI + PWA）

上傳 PDF / DOCX，以多種檢索策略找出相關段落，再由 Groq 模型生成附出處的答案。可匯出問答紀錄為 Word 或 CSV，並可安裝到手機或電腦桌面。

## 功能
- 📄 支援 PDF（逐頁）與 DOCX（依標題與表格切分）
- ⚡ 8 種檢索策略：混合搜尋、關鍵字、語意、重新排序、多查詢、壓縮、父子文檔、HyDE
- 🧠 Groq 模型生成答案，並標註 `[片段n]` 出處
- 📤 匯出 Word（含參考片段）與 CSV
- 📱 RWD 響應式介面與 PWA（可加入主畫面、離線提示頁）

## 專案結構
```
.
├── app.py                  # Gradio 介面 + FastAPI + PWA 路由
├── requirements.txt
├── render.yaml             # Render Blueprint
├── .python-version
├── static/
│   ├── manifest.webmanifest
│   ├── sw.js
│   ├── offline.html
│   └── icons/
└── tools/make_icons.py     # 重新產生圖示（選用）
```

## 本機執行
```bash
pip install -r requirements.txt
export GROQ_API_KEY=gsk_xxx     # Windows PowerShell: $env:GROQ_API_KEY="gsk_xxx"
python app.py                   # 本機會產生 gradio.live 公開連結
# 或指定埠號：PORT=7860 uvicorn app:app --host 0.0.0.0 --port 7860
```

## Google Colab
```python
!pip install -q -r requirements.txt   # 或只裝核心套件
import os; os.environ["GROQ_API_KEY"] = "gsk_xxx"
```
接著貼上 `app.py` 執行，會以 `share=True` 產生公開連結。

## 部署到 Render
1. 將此資料夾的**內容**（非資料夾本身）推送到 GitHub 倉庫根目錄。
2. Render → **New +** → **Blueprint**，選擇倉庫（會讀取 `render.yaml`）。
3. 在 Render 的 **Environment** 填入 `GROQ_API_KEY`。
4. 部署完成後開啟網址，確認 `/healthz` 顯示 `"has_key": true`。

手動建立 Web Service 時設定：
- Build Command：`pip install -r requirements.txt`
- Start Command：`uvicorn app:app --host 0.0.0.0 --port $PORT`

## 記憶體與語意檢索
- 預設 `USE_EMBEDDINGS=0`，只用關鍵字（TF-IDF）與 LLM 相關策略，適合 Render 免費方案（512 MB）。
- 若要啟用語意向量檢索，設定 `USE_EMBEDDINGS=1`，並建議使用付費方案。`EMBED_MODEL` 可指定其他 fastembed 支援的模型。
- 語意策略在未啟用時會自動改用關鍵字檢索，不會報錯。

## 注意事項
- Render 免費方案磁碟為暫存，重新部署後已上傳的文件會消失，需重新上傳。
- 文件與對話紀錄存在伺服器記憶體中，所有使用者共用同一份狀態；若需多人同時使用，請再加上 session 管理。
- 修改 `static/` 內容後，請把 `static/sw.js` 的 `CACHE` 版本號改掉。
- PWA 需要 HTTPS，Render 預設提供。
- 免費方案閒置後會休眠，首次開啟需等待數十秒。
