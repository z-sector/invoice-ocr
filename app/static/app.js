(() => {
  const FIELDS = [
    "vendor_name",
    "invoice_number",
    "amount",
    "currency",
    "bank_name_on_invoice",
    "account_number_on_invoice",
    "routing_on_invoice",
  ];

  const $ = (id) => document.getElementById(id);
  const dropzone = $("dropzone");
  const fileInput = $("file-input");
  const preview = $("preview");
  const pdfFrame = $("pdf-preview");
  const fileName = $("file-name");
  const clearBtn = $("clear-btn");
  const parseBtn = $("parse-btn");
  const statusEl = $("status");
  const resultEmpty = $("result-empty");
  const resultError = $("result-error");
  const result = $("result");
  const jsonOut = $("json-out");
  const fieldsBody = $("fields-body");
  const rawText = $("raw-text");
  const metaLine = $("meta-line");
  const copyBtn = $("copy-btn");

  let currentFile = null;
  let lastJson = null;
  let previewUrl = null;

  function isPdf(file) {
    const name = (file.name || "").toLowerCase();
    return file.type === "application/pdf" || file.type === "application/x-pdf" || name.endsWith(".pdf");
  }

  function isImage(file) {
    const name = (file.name || "").toLowerCase();
    return file.type.startsWith("image/") || /\.(jpe?g|png|webp|bmp|tiff?|gif)$/.test(name);
  }

  function setStatus(text, kind) {
    statusEl.textContent = text || "";
    statusEl.className = "status" + (kind ? " status-" + kind : "");
  }

  function showError(message) {
    resultError.textContent = message;
    resultError.hidden = false;
    result.hidden = true;
    resultEmpty.hidden = true;
    copyBtn.hidden = true;
  }

  function setFile(file) {
    if (!file) return;
    const pdf = isPdf(file);
    if (!pdf && !isImage(file)) {
      setStatus("Нужно изображение (JPG, PNG, WEBP, BMP, TIFF) или PDF.", "error");
      return;
    }
    currentFile = file;
    fileName.textContent = `${file.name} · ${(file.size / 1024).toFixed(0)} КБ`;
    clearBtn.hidden = false;
    parseBtn.disabled = false;
    setStatus("");
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(file);
    preview.hidden = pdf;
    pdfFrame.hidden = !pdf;
    if (pdf) {
      preview.removeAttribute("src");
      pdfFrame.src = previewUrl;
    } else {
      pdfFrame.removeAttribute("src");
      preview.src = previewUrl;
    }
    dropzone.classList.add("has-preview");
  }

  function clearFile() {
    currentFile = null;
    fileInput.value = "";
    preview.hidden = true;
    preview.removeAttribute("src");
    pdfFrame.hidden = true;
    pdfFrame.removeAttribute("src");
    if (previewUrl) {
      URL.revokeObjectURL(previewUrl);
      previewUrl = null;
    }
    dropzone.classList.remove("has-preview");
    fileName.textContent = "Файл не выбран";
    clearBtn.hidden = true;
    parseBtn.disabled = true;
    setStatus("");
  }

  function renderResult(data) {
    lastJson = data.invoice;
    jsonOut.textContent = JSON.stringify(data.invoice, null, 2);

    fieldsBody.innerHTML = "";
    for (const key of FIELDS) {
      const value = data.invoice[key] || "";
      const tr = document.createElement("tr");
      const tdKey = document.createElement("td");
      tdKey.innerHTML = `<code>${key}</code>`;
      const tdVal = document.createElement("td");
      tdVal.textContent = value || "—";
      tdVal.className = value ? "val" : "val val-empty";
      const tdSrc = document.createElement("td");
      tdSrc.textContent = (data.sources && data.sources[key]) || "";
      tdSrc.className = "src";
      tr.append(tdKey, tdVal, tdSrc);
      fieldsBody.appendChild(tr);
    }

    rawText.textContent = (data.ocr.lines || []).join("\n") || "(текст не найден)";
    const pagesInFile = data.ocr.pages_in_file || data.ocr.page_count || 1;
    const pagesRead = data.ocr.page_count || 1;
    let pages = "";
    if ((data.file && data.file.kind === "pdf") || pagesInFile > 1) {
      pages = pagesRead < pagesInFile
        ? ` · стр. 1–${pagesRead} из ${pagesInFile}`
        : ` · ${pagesInFile} стр.`;
    }
    metaLine.textContent =
      `· ${data.ocr.line_count} строк${pages} · уверенность ${(data.ocr.mean_confidence * 100).toFixed(0)}% · ${data.ocr.total_ms} мс`;

    resultEmpty.hidden = true;
    resultError.hidden = true;
    result.hidden = false;
    copyBtn.hidden = false;
  }

  async function parse() {
    if (!currentFile) return;
    parseBtn.disabled = true;
    parseBtn.classList.add("is-loading");
    const pdf = isPdf(currentFile);
    setStatus(pdf
      ? "Распознаём страницы PDF на CPU… обычно несколько секунд на страницу."
      : "Распознаём текст на CPU… обычно 3–20 секунд.", "info");

    const form = new FormData();
    form.append("file", currentFile, currentFile.name);

    try {
      const resp = await fetch("/api/parse", { method: "POST", body: form });
      let payload = null;
      try {
        payload = await resp.json();
      } catch (_) {
        payload = null;
      }
      if (!resp.ok) {
        const detail = (payload && payload.detail) || `Ошибка сервера (${resp.status})`;
        showError(typeof detail === "string" ? detail : JSON.stringify(detail));
        setStatus("Не удалось обработать файл.", "error");
        return;
      }
      renderResult(payload);
      const filled = FIELDS.filter((k) => payload.invoice[k]).length;
      let done = `Готово: заполнено ${filled} из ${FIELDS.length} полей.`;
      const pagesInFile = payload.ocr.pages_in_file || 0;
      const pagesRead = payload.ocr.page_count || 0;
      if (pagesInFile > pagesRead) {
        done += ` Обработаны первые ${pagesRead} из ${pagesInFile} страниц.`;
      }
      setStatus(done, "ok");
    } catch (err) {
      showError("Сервер недоступен: " + (err && err.message ? err.message : err));
      setStatus("Не удалось обработать файл.", "error");
    } finally {
      parseBtn.disabled = !currentFile;
      parseBtn.classList.remove("is-loading");
    }
  }

  fileInput.addEventListener("change", () => setFile(fileInput.files[0]));
  clearBtn.addEventListener("click", clearFile);
  parseBtn.addEventListener("click", parse);

  ["dragenter", "dragover"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.add("is-dragover");
    })
  );
  // The PDF preview is its own document, so a drag over it never reaches the dropzone.
  // While a file is dragged anywhere, let the drop land on the dropzone instead.
  document.addEventListener("dragover", (e) => {
    const types = e.dataTransfer && e.dataTransfer.types;
    if (!types || ![...types].includes("Files")) return;
    e.preventDefault();
    dropzone.classList.add("is-dragover");
  });
  document.addEventListener("dragleave", (e) => {
    if (!e.relatedTarget) dropzone.classList.remove("is-dragover");
  });
  document.addEventListener("drop", (e) => {
    if (e.target !== dropzone && !dropzone.contains(e.target)) dropzone.classList.remove("is-dragover");
  });
  ["dragleave", "drop"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.remove("is-dragover");
    })
  );
  dropzone.addEventListener("drop", (e) => {
    const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    setFile(file);
  });

  document.addEventListener("paste", (e) => {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    for (const item of items) {
      if (item.type.startsWith("image/")) {
        const file = item.getAsFile();
        if (file) {
          setFile(new File([file], file.name || "clipboard.png", { type: file.type }));
          break;
        }
      }
    }
  });

  copyBtn.addEventListener("click", async () => {
    if (!lastJson) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(lastJson, null, 2));
      copyBtn.textContent = "Скопировано";
      setTimeout(() => (copyBtn.textContent = "Копировать JSON"), 1500);
    } catch (_) {
      copyBtn.textContent = "Не удалось скопировать";
      setTimeout(() => (copyBtn.textContent = "Копировать JSON"), 1500);
    }
  });
})();
