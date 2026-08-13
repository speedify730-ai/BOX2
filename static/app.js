(function () {
  "use strict";

  const form = document.getElementById("dubForm");
  const formError = document.getElementById("formError");
  const submitBtn = document.getElementById("submitBtn");
  const monitor = document.getElementById("monitor");
  const pctEl = document.getElementById("pct");
  const statusMsg = document.getElementById("statusMsg");
  const resultArea = document.getElementById("resultArea");

  // ---------- Accordions (Advanced settings sections) ----------
  // Uses the CSS grid-template-rows 0fr -> 1fr trick so the panel animates
  // open/closed smoothly without JS height measurement, and works reliably
  // across all browsers (unlike <details>/<summary> which is what this
  // replaced).
  document.querySelectorAll("[data-accordion]").forEach((btn) => {
    const target = document.getElementById(btn.getAttribute("data-accordion"));
    const icon = btn.querySelector(".accordion-icon");
    if (!target) return;
    btn.addEventListener("click", () => {
      const isOpen = btn.getAttribute("aria-expanded") === "true";
      btn.setAttribute("aria-expanded", String(!isOpen));
      target.classList.toggle("grid-rows-[0fr]", isOpen);
      target.classList.toggle("grid-rows-[1fr]", !isOpen);
      if (icon) icon.classList.toggle("rotate-180", !isOpen);
    });
  });

  // ---------- Dropzone + input video preview ----------
  const dropzone = document.getElementById("dropzone");
  const videoInput = document.getElementById("videoInput");
  const dzIdle = document.getElementById("dzIdle");
  const inputPreviewWrap = document.getElementById("inputPreviewWrap");
  const inputPreview = document.getElementById("inputPreview");
  const inputFileName = document.getElementById("inputFileName");
  const changeVideoBtn = document.getElementById("changeVideoBtn");

  let inputObjectUrl = null;

  function humanSize(bytes) {
    if (bytes > 1024 * 1024 * 1024) return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function showVideoPreview(file) {
    if (!file) return;
    if (inputObjectUrl) URL.revokeObjectURL(inputObjectUrl);
    inputObjectUrl = URL.createObjectURL(file);
    inputPreview.src = inputObjectUrl;
    inputFileName.textContent = file.name + " (" + humanSize(file.size) + ")";
    dzIdle.classList.add("hidden");
    inputPreviewWrap.classList.remove("hidden");
  }

  videoInput.addEventListener("change", () => showVideoPreview(videoInput.files[0]));

  ["dragenter", "dragover"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add("border-cyan-400/60"); })
  );
  ["dragleave", "drop"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.remove("border-cyan-400/60"); })
  );
  dropzone.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files[0];
    if (f) {
      videoInput.files = e.dataTransfer.files;
      showVideoPreview(f);
    }
  });

  changeVideoBtn.addEventListener("click", (e) => {
    e.preventDefault();
    videoInput.value = "";
    if (inputObjectUrl) { URL.revokeObjectURL(inputObjectUrl); inputObjectUrl = null; }
    inputPreview.removeAttribute("src");
    inputPreviewWrap.classList.add("hidden");
    dzIdle.classList.remove("hidden");
  });

  // ---------- Show/hide dependent sections ----------
  function bindToggle(toggleId, sectionId) {
    const toggle = document.getElementById(toggleId);
    const section = document.getElementById(sectionId);
    function sync() { section.classList.toggle("hidden", !toggle.checked); }
    toggle.addEventListener("change", sync);
    sync();
  }
  bindToggle("subtitlesToggle", "subtitleOptions");
  bindToggle("titleToggle", "titleOptions");

  // ---------- Test keys ----------
  const testKeysBtn = document.getElementById("testKeysBtn");
  const keyCheckResult = document.getElementById("keyCheckResult");

  function keyChip(ok, text) {
    const dotColor = ok ? "bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.9)]" : "bg-rose-400 shadow-[0_0_6px_rgba(251,113,133,0.9)]";
    return `<div class="flex items-center gap-2"><span class="w-1.5 h-1.5 rounded-full ${dotColor}"></span><span class="text-slate-400">${text}</span></div>`;
  }

  testKeysBtn.addEventListener("click", async () => {
    const gemini = document.getElementById("geminiKeys").value;
    const groq = document.getElementById("groqKeys").value;
    if (!gemini.trim() && !groq.trim()) {
      keyCheckResult.innerHTML = keyChip(false, "Key ထည့်ပါ");
      return;
    }
    testKeysBtn.disabled = true;
    testKeysBtn.textContent = "⏳ စစ်ဆေးနေသည်...";
    keyCheckResult.innerHTML = "";
    try {
      const res = await fetch("/api/check-keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ gemini, groq }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Error");
      let html = "";
      (data.gemini || []).forEach((k) => {
        html += keyChip(k.ok, `Gemini ${k.key} ${k.ok ? "OK" : "(" + (k.err || "error") + ")"}`);
      });
      (data.groq || []).forEach((k) => {
        html += keyChip(k.ok, `Groq ${k.key} ${k.ok ? "OK" : "(" + (k.err || "error") + ")"}`);
      });
      keyCheckResult.innerHTML = html || keyChip(false, "Key မတွေ့ပါ");
    } catch (e) {
      keyCheckResult.innerHTML = keyChip(false, e.message);
    } finally {
      testKeysBtn.disabled = false;
      testKeysBtn.textContent = "🔑 Key စစ်ဆေးမည်";
    }
  });

  // ---------- Submit / job polling ----------
  let pollTimer = null;

  function setMonitor(state, pct, msg) {
    pctEl.textContent = (pct === null || pct === undefined) ? "--%" : pct + "%";
    statusMsg.textContent = msg || "";
    monitor.classList.toggle("monitor-active", state === "processing" || state === "queued");
  }

  function showResult(kind, payload) {
    if (kind === "done") {
      resultArea.innerHTML = `
        <video id="outputPreview" controls class="w-full rounded-xl border border-emerald-400/30 shadow-[0_0_24px_-6px_rgba(52,211,153,0.35)] bg-black"></video>
        <div class="flex flex-col sm:flex-row gap-2 mt-3">
          <a href="${payload.download_url}" download
             class="flex-1 text-center font-tech text-sm font-semibold px-4 py-2.5 rounded-lg bg-emerald-400/15 border border-emerald-400/40 text-emerald-300 hover:bg-emerald-400/25 transition-colors">
            ⬇ ဒေါင်းလုတ်လုပ်မည်
          </a>
          <button type="button" id="resetBtn"
             class="font-tech text-sm px-4 py-2.5 rounded-lg border border-white/10 text-slate-400 hover:border-fuchsia-400/50 hover:text-fuchsia-300 transition-colors">
            နောက်တစ်ခု ဒပ်ဘ်လုပ်မည်
          </button>
        </div>`;
      const outputPreview = document.getElementById("outputPreview");
      if (outputPreview && payload.watch_url) outputPreview.src = payload.watch_url;
    } else if (kind === "error") {
      resultArea.innerHTML = `
        <div class="rounded-lg border border-rose-400/40 bg-rose-400/10 text-rose-300 text-[13px] px-4 py-3">${payload}</div>
        <button type="button" id="resetBtn"
           class="w-full mt-3 font-tech text-sm px-4 py-2.5 rounded-lg border border-white/10 text-slate-400 hover:border-fuchsia-400/50 hover:text-fuchsia-300 transition-colors">
          ပြန်ကြိုးစားမည်
        </button>`;
    } else {
      resultArea.innerHTML = "";
    }
    const resetBtn = document.getElementById("resetBtn");
    if (resetBtn) resetBtn.addEventListener("click", resetForm);
  }

  function resetForm() {
    setMonitor("idle", null, "Video တင်ပြီး ဒပ်ဘ်စတင်ရန် ဘေးက form ကို ဖြည့်ပါ။");
    showResult("idle");
    submitBtn.disabled = false;
    submitBtn.textContent = "▶ ဒပ်ဘ် စတင်မည်";
    if (pollTimer) clearInterval(pollTimer);
  }

  function pollJob(jobId) {
    pollTimer = setInterval(async () => {
      try {
        const res = await fetch(`/api/jobs/${jobId}`);
        if (!res.ok) throw new Error("Job status unavailable");
        const data = await res.json();
        setMonitor(data.status, data.progress, data.message);
        if (data.status === "done") {
          clearInterval(pollTimer);
          submitBtn.disabled = false;
          submitBtn.textContent = "▶ ဒပ်ဘ် စတင်မည်";
          showResult("done", data);
        } else if (data.status === "error") {
          clearInterval(pollTimer);
          submitBtn.disabled = false;
          submitBtn.textContent = "▶ ဒပ်ဘ် စတင်မည်";
          setMonitor("error", data.progress, "မအောင်မြင်ပါ");
          showResult("error", data.message);
        }
      } catch (e) {
        clearInterval(pollTimer);
        submitBtn.disabled = false;
        submitBtn.textContent = "▶ ဒပ်ဘ် စတင်မည်";
        setMonitor("error", null, "ချိတ်ဆက်မှု အမှား");
        showResult("error", e.message);
      }
    }, 2500);
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    formError.classList.add("hidden");
    if (!videoInput.files[0]) {
      formError.textContent = "Video file ရွေးပါ။";
      formError.classList.remove("hidden");
      return;
    }
    submitBtn.disabled = true;
    submitBtn.textContent = "⏳ တင်နေသည်...";
    showResult("idle");
    setMonitor("queued", 0, "Upload လုပ်နေသည်...");

    const fd = new FormData(form);
    try {
      const res = await fetch("/api/jobs", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "တင်ခြင်း မအောင်မြင်ပါ");
      setMonitor("queued", 0, "စီစဉ်နေသည်...");
      pollJob(data.job_id);
    } catch (err) {
      submitBtn.disabled = false;
      submitBtn.textContent = "▶ ဒပ်ဘ် စတင်မည်";
      formError.textContent = err.message;
      formError.classList.remove("hidden");
      setMonitor("idle", null, "Video တင်ပြီး ဒပ်ဘ်စတင်ရန် ဘေးက form ကို ဖြည့်ပါ။");
    }
  });
})();
