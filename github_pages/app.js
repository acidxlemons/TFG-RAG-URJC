// Typewriter effect for the terminal
const lines = [
    { text: '$ docker compose up -d', cls: 'cmd' },
    { text: '[+] Running 16/16 services started', cls: 'success' },
    { text: '', cls: '' },
    { text: '🧠 JARVIS Pipeline ready on :9099', cls: 'success' },
    { text: '📡 Backend RAG ready on :8002', cls: 'success' },
    { text: '🤖 Ollama loaded: rag-qwen-ft, qwen2.5vl:7b', cls: 'info' },
    { text: '🧮 Qdrant: 2,340 vectors indexed', cls: 'info' },
    { text: '', cls: '' },
    { text: '> User: ¿Cuál es la política de calidad?', cls: 'cmd' },
    { text: '  [JARVIS] Intent: RAG | Collections: documents', cls: 'info' },
    { text: '  [Backend] Embedding → Qdrant → 5 chunks (0.08s)', cls: 'info' },
    { text: '  [LiteLLM] → Ollama → rag-qwen-ft (streaming)', cls: 'info' },
    { text: '  ✓ Response: 247 tokens in 3.2s', cls: 'success' },
    { text: '  📄 Sources: MC-01.pdf (p.3), MC-01.pdf (p.7)', cls: 'info' },
    { text: '', cls: '' },
    { text: '> User: Busca en el BOE la ley de protección de datos', cls: 'cmd' },
    { text: '  [JARVIS] Intent: BOE | Resolving...', cls: 'info' },
    { text: '  [MCP-BOE] "protección de datos" → BOE-A-2018-16673', cls: 'success' },
    { text: '  ✓ LOPDGDD retrieved and summarized', cls: 'success' },
    { text: '', cls: '' },
    { text: '> User: ¿Cuántos documentos indexados esta semana?', cls: 'cmd' },
    { text: '  [JARVIS] Intent: SQL | auto-routing v2.1...', cls: 'info' },
    { text: '  [SQLAgent] SELECT COUNT(*) WHERE created_at >= NOW()-7d', cls: 'info' },
    { text: '  ✓ Resultado: 47 documentos. Respondiendo...', cls: 'success' },
];

const container = document.getElementById('typewriter');
let lineIdx = 0;
let charIdx = 0;
let currentLineEl = null;

function typeNext() {
    if (lineIdx >= lines.length) {
        // Restart after pause
        setTimeout(() => {
            container.innerHTML = '';
            lineIdx = 0;
            charIdx = 0;
            currentLineEl = null;
            typeNext();
        }, 4000);
        return;
    }

    const line = lines[lineIdx];

    if (!currentLineEl) {
        currentLineEl = document.createElement('div');
        currentLineEl.className = `log-line ${line.cls}`;
        container.appendChild(currentLineEl);
    }

    if (charIdx < line.text.length) {
        currentLineEl.textContent += line.text[charIdx];
        charIdx++;
        // Scroll to bottom
        container.scrollTop = container.scrollHeight;
        setTimeout(typeNext, line.cls === 'cmd' ? 35 : 12);
    } else {
        lineIdx++;
        charIdx = 0;
        currentLineEl = null;
        setTimeout(typeNext, line.text === '' ? 200 : 400);
    }
}

// Start when page loads
document.addEventListener('DOMContentLoaded', () => {
    setTimeout(typeNext, 800);
});

// Tilt effect on feature cards
document.querySelectorAll('.tilt-card').forEach(card => {
    card.addEventListener('mousemove', (e) => {
        const rect = card.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        const centerX = rect.width / 2;
        const centerY = rect.height / 2;
        const rotateX = (y - centerY) / 20;
        const rotateY = (centerX - x) / 20;
        card.style.transform = `perspective(800px) rotateX(${rotateX}deg) rotateY(${rotateY}deg) scale(1.02)`;
    });
    card.addEventListener('mouseleave', () => {
        card.style.transform = 'perspective(800px) rotateX(0) rotateY(0) scale(1)';
    });
});

// Smooth scroll
document.querySelectorAll('a[href^="#"]').forEach(anchor => {
    anchor.addEventListener('click', function (e) {
        e.preventDefault();
        const target = document.querySelector(this.getAttribute('href'));
        if (target) {
            target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
    });
});
