# Proactive Test Lab First 3 Steps Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the first 3 steps of the Proactive Test Lab UI to provide a clean, visual, auto-advancing step-by-step workflow (Import -> Select -> Generate) that prevents user confusion by hiding future steps until unlocked.

**Architecture:** Single Page Application interface in `app/static/proactive_test_lab.html`. Split into three steps managed by `currentStep` (1, 2, 3) in JavaScript. Step 1 shows an overlay modal/dashboard for ingestion. Step 2 shows the Sample Pool queue with full-card clickable selection. Step 3 enables LLM generation and auto-forwards to the Review Queue.

**Tech Stack:** Vanilla JavaScript (ES6), CSS3 Variables, standard DOM APIs, HTML5.

## Global Constraints

*   Target Path: `app/static/proactive_test_lab.html`
*   Option A sandbox constraints (no live WeChat dispatch).
*   All smart curly quotes (`“`, `”`) must remain standard straight double quotes (`"`).
*   All backend API contracts must be fully preserved.

---

### Task 1: Stepper Structure, Step Isolation CSS, and Global State

**Files:**
*   Modify: `app/static/proactive_test_lab.html`

**Interfaces:**
*   Produces: `setStep(step)` JS function, step panel DOM nodes, stepper layout styling.

*   [ ] **Step 1: Declare Step Navigation Styles**
    Add CSS to support step isolation and styling of the visual stepper:
    ```css
    /* Stepper Navigation Header */
    .stepper-header {
      padding: 10px;
      border-bottom: 1px solid var(--line);
      background: var(--panel-soft);
    }
    .stepper-tabs {
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .step-tab {
      flex: 1;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 6px 2px;
      border-radius: var(--border-radius-sm);
      background: var(--panel-soft);
      border: 1px solid var(--line);
      color: var(--muted);
      cursor: not-allowed;
      opacity: 0.5;
      transition: all 0.2s ease;
      position: relative;
    }
    .step-tab.unlocked {
      cursor: pointer;
      opacity: 0.9;
    }
    .step-tab.unlocked:hover {
      background: var(--line-soft);
      border-color: var(--line-strong);
    }
    .step-tab.active {
      background: #ffffff;
      border-color: var(--primary);
      color: var(--primary);
      box-shadow: 0 2px 4px rgba(55, 95, 114, 0.08);
      font-weight: 600;
      opacity: 1;
      cursor: pointer;
    }
    .step-badge {
      width: 16px;
      height: 16px;
      border-radius: 50%;
      background: var(--line-strong);
      color: #ffffff;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 9px;
      font-weight: 700;
      margin-bottom: 2px;
    }
    .step-tab.active .step-badge {
      background: var(--primary);
    }
    .step-tab.unlocked .step-badge {
      background: var(--primary-line);
      color: var(--primary);
    }
    .step-label {
      font-size: 10px;
    }
    .step-arrow {
      color: var(--line-strong);
      font-size: 9px;
    }
    .step-count {
      position: absolute;
      top: -3px;
      right: -3px;
      background: var(--line-strong);
      color: #ffffff;
      font-size: 8px;
      font-weight: 700;
      padding: 1px 3px;
      border-radius: 999px;
      min-width: 12px;
      text-align: center;
    }
    .step-tab.active .step-count {
      background: var(--primary);
    }
    ```

*   [ ] **Step 2: Add CSS for Modal Overlay Ingestion Dashboard**
    Add styles for Step 1 modal to avoid layout clutter:
    ```css
    .step-modal-overlay {
      position: absolute;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background: rgba(248, 249, 252, 0.98);
      z-index: 100;
      display: flex;
      flex-direction: column;
      padding: 15px;
      overflow-y: auto;
    }
    .import-options-grid {
      display: flex;
      flex-direction: column;
      gap: 12px;
      margin-top: 10px;
    }
    .import-card {
      background: #ffffff;
      border: 1px solid var(--line);
      border-radius: var(--border-radius-md);
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      transition: all 0.15s ease;
    }
    .import-card:hover {
      border-color: var(--primary-line);
      box-shadow: var(--shadow-sm);
    }
    .import-card h4 {
      margin: 0;
      font-size: 13px;
      font-weight: 700;
      color: var(--text);
    }
    .import-card p {
      margin: 0;
      font-size: 11px;
      color: var(--muted);
      line-height: 1.4;
    }
    .file-upload-zone {
      border: 1px dashed var(--line-strong);
      border-radius: var(--border-radius-sm);
      padding: 12px;
      text-align: center;
      background: var(--panel-soft);
      cursor: pointer;
    }
    ```

*   [ ] **Step 3: Define DOM Layout Structures**
    Replace `<div class="tabs">` and `<div class="left-rail-body">` contents to incorporate the Stepper Header and three isolated panels: `#panelStep1` (overlay modal), `#panelStep2` (Sample list), and `#panelStep3` (Candidate queue).

*   [ ] **Step 4: Implement Step State JS Controller**
    Add global step variables and state management function:
    ```javascript
    let maxUnlockedStep = 1; // Tracks progress
    
    function setStep(step) {
      if (step > maxUnlockedStep) return; // Prevent clicking locked future steps
      
      // Hide all panels
      document.getElementById('panelStep1').style.display = 'none';
      document.getElementById('panelStep2').style.display = 'none';
      document.getElementById('panelStep3').style.display = 'none';
      document.getElementById('generateBar').style.display = 'none';

      // Remove active classes
      for (let i = 1; i <= 3; i++) {
        const btn = document.getElementById(`btnStep${i}`);
        if (btn) {
          btn.classList.remove('active');
          if (i <= maxUnlockedStep) {
            btn.classList.add('unlocked');
          } else {
            btn.classList.remove('unlocked');
          }
        }
      }

      // Show requested step
      document.getElementById(`btnStep${step}`).classList.add('active');
      if (step === 1) {
        document.getElementById('panelStep1').style.display = 'flex';
      } else if (step === 2) {
        document.getElementById('panelStep2').style.display = 'flex';
        document.getElementById('generateBar').style.display = 'flex';
        loadSamples();
      } else if (step === 3) {
        document.getElementById('panelStep3').style.display = 'flex';
        loadCandidates();
      }
      renderQueue();
    }
    
    function unlockStep(step) {
      if (step > maxUnlockedStep) {
        maxUnlockedStep = step;
      }
      setStep(step);
    }
    
    function resetWorkflow() {
      maxUnlockedStep = 1;
      setStep(1);
      // Clear inputs
      document.getElementById('sampleFile').value = '';
      document.getElementById('fileUploadStatus').textContent = '点击选择 JSON / JSONL 文件';
      document.getElementById('realAccountId').value = '';
    }
    ```

*   [ ] **Step 5: Run quick syntax verification**
    Save and run pytest command to verify backend endpoints and baseline test pass rate.

---

### Task 2: Step 1 Modal Import Dashboard & Auto-Advancing Ingestion

**Files:**
*   Modify: `app/static/proactive_test_lab.html`

**Interfaces:**
*   Consumes: `bootstrapSamples()`, `importFile()`, `createSampleFromSession()`
*   Produces: Automated routing `unlockStep(2)` on success actions.

*   [ ] **Step 1: Write Ingestion Card Templates inside #panelStep1**
    Embed the cards for Synthetic Ingestion, File Import (with styled File Zone), and Session pull form. Add dynamic file name change callback:
    ```javascript
    function handleFileSelect(input) {
      const status = document.getElementById('fileUploadStatus');
      if (input.files && input.files[0]) {
        status.textContent = `已选择: ${input.files[0].name}`;
        status.style.color = 'var(--primary)';
      }
    }
    ```

*   [ ] **Step 2: Connect Auto-Advance hooks to Import callbacks**
    Update the promise callbacks of `bootstrapSamples()`, `importFile()`, and `createSampleFromSession()`:
    Upon successful responses, trigger `unlockStep(2)` to advance the user to Step 2, and call `refreshAll()` to reload the tables.
    ```javascript
    // Example hook update inside bootstrapSamples
    setStatus('样本套入成功！正在前往第二步挑选样本...', 'ok');
    unlockStep(2);
    ```

*   [ ] **Step 3: Add Global Reset/Restart Triggers**
    Place a "重新导入/添加新样本" button on Step 2 to allow the user to easily open the Step 1 overlay modal anytime.

*   [ ] **Step 4: Run import callbacks test**
    Verify import functions execute correctly. Run pytest command.

---

### Task 3: Step 2 Sample Card Click-to-Select Layout Redesign

**Files:**
*   Modify: `app/static/proactive_test_lab.html`

**Interfaces:**
*   Consumes: `renderSampleCard(sample)`
*   Produces: Interactive card selection binding, selection count indicators.

*   [ ] **Step 1: Redesign renderSampleCard HTML**
    Move the checkbox to the left front-end side of the card, remove the text "勾选此样本", and add classes for active state selection:
    ```javascript
    function renderSampleCard(sample) {
      const active = selectedSampleId === sample.sample_id ? ' active' : '';
      const checked = isSampleChecked(sample.sample_id) ? 'checked' : '';
      const preview = (sample.chat_history || []).slice(-3).map(m => `<span>${roleLabel(m.role)}: ${shortText(messageText(m), 25)}</span>`).join(' / ');
      return `<article class="sample-card${active}" onclick="handleSampleCardClick(event, '${esc(sample.sample_id)}')">
        <div style="display:flex; align-items:center; gap:8px;">
          <input class="sampleCheck" type="checkbox" value="${esc(sample.sample_id)}" ${checked} onclick="event.stopPropagation(); updateGenerateBtn();">
          <div class="card-identity">
            <strong>${esc(sample.sample_id)}</strong>
            <span class="subtle">${esc(sample.source || '-')} · ${esc(sample.silence_hours || 0)}h</span>
          </div>
        </div>
        <div class="card-meta-row" style="margin-left: 24px;">
          <span class="pill primary">${esc(scenarioLabel(sample.scenario_type))}</span>
        </div>
        <div class="card-draft-preview" style="margin-left: 24px;">${preview || '无对话'}</div>
      </article>`;
    }
    ```

*   [ ] **Step 2: Add Click Toggle & Select All Logic**
    Bind full card click to selection change, update counters automatically:
    ```javascript
    function handleSampleCardClick(event, sampleId) {
      // If clicking checkbox itself, stop duplication
      if (event.target.classList.contains('sampleCheck')) return;
      
      const card = event.currentTarget;
      const cb = card.querySelector('.sampleCheck');
      if (cb) {
        cb.checked = !cb.checked;
        updateGenerateBtn();
      }
      selectSample(sampleId);
    }
    
    function toggleSelectAllSamples(checkbox) {
      const checkboxes = document.querySelectorAll('#sampleQueueBody .sampleCheck');
      checkboxes.forEach(cb => {
        cb.checked = checkbox.checked;
      });
      updateGenerateBtn();
    }
    ```

*   [ ] **Step 3: Update Selection Checkbox Tracker**
    Add local helper array to ensure checked statuses are maintained across scroll/renders:
    ```javascript
    let checkedSampleIdsCache = new Set();
    function isSampleChecked(id) {
      return checkedSampleIdsCache.has(id);
    }
    function updateGenerateBtn() {
      // Sync DOM checkbox state to Set
      document.querySelectorAll('#sampleQueueBody .sampleCheck').forEach(cb => {
        if (cb.checked) checkedSampleIdsCache.add(cb.value);
        else checkedSampleIdsCache.delete(cb.value);
      });
      
      const count = checkedSampleIdsCache.size;
      const btn = document.getElementById('generateBtn');
      if (btn) {
        btn.textContent = count > 0 ? `生成选中 ${count} 个样本的候选 ➔` : '生成选中样本的候选';
        btn.disabled = count === 0;
      }
      const counterText = document.getElementById('selectionCounterText');
      if (counterText) {
        counterText.textContent = `已选 ${count} 个样本`;
      }
    }
    ```

*   [ ] **Step 4: Verify Selection Flow passes browser tests**
    Run pytest command to confirm all endpoints remain healthy.

---

### Task 4: Step 3 Candidate Generation & Loading State Overlay

**Files:**
*   Modify: `app/static/proactive_test_lab.html`

**Interfaces:**
*   Consumes: `runGeneration()`
*   Produces: Interactive generation spinner, `unlockStep(3)` forwarding layout.

*   [ ] **Step 1: Inject Progress Spinner CSS**
    Add animation rules for generation loading overlay:
    ```css
    .progress-spinner {
      border: 3px solid rgba(255,255,255,0.3);
      border-radius: 50%;
      border-top: 3px solid #fff;
      width: 16px;
      height: 16px;
      animation: spin 1s linear infinite;
      display: inline-block;
      margin-right: 8px;
      vertical-align: middle;
    }
    @keyframes spin {
      0% { transform: rotate(0deg); }
      100% { transform: rotate(360deg); }
    }
    ```

*   [ ] **Step 2: Add Spinner to runGeneration JS flow**
    Show interactive loading state directly on the action bar when calling the LLM generator:
    ```javascript
    async function runGeneration() {
      const ids = Array.from(checkedSampleIdsCache);
      if (!ids.length) return alert('请先在上方勾选样本！');
      
      const btn = document.getElementById('generateBtn');
      const statusEl = document.getElementById('generateStatus');
      btn.disabled = true;
      btn.innerHTML = `<span class="progress-spinner"></span>正在生成候选文案...`;
      statusEl.textContent = '模型正在并发生成中，这可能需要几秒钟...';
      
      try {
        const out = await api('/generate', {method:'POST', body:JSON.stringify({sample_ids: ids, dry_run: true})});
        statusEl.textContent = `生成完成：成功 ${out.success}，跳过 ${out.skip}，失败 ${out.error}`;
        statusEl.className = 'status-message ok';
        
        // Auto-unlock and forward to Step 3
        unlockStep(3);
        
        // Auto-select the first candidate to start review immediately
        await loadCandidates();
        if (candidates.length > 0) {
          selectCandidate(candidates[0].id);
        }
      } catch (err) {
        statusEl.textContent = '生成失败: ' + err.message;
        statusEl.className = 'status-message error';
      } finally {
        btn.disabled = false;
        updateGenerateBtn();
      }
    }
    ```

*   [ ] **Step 3: Verify the entire stepper lifecycle passes execution**
    Run pytest command to confirm all test paths.
