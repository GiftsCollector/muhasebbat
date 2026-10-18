(function () {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const root = document.documentElement;
  const canvas = document.getElementById("cinematicParticles");

  function setPointer(x, y) {
    root.style.setProperty("--mx", (x / window.innerWidth) * 100 + "%");
    root.style.setProperty("--my", (y / window.innerHeight) * 100 + "%");
  }

  window.addEventListener("pointermove", function (event) {
    setPointer(event.clientX, event.clientY);
  }, { passive: true });

  function runParticles() {
    if (reduced || !canvas || !canvas.getContext) return;
    const ctx = canvas.getContext("2d");
    const dots = [];
    let width = 0;
    let height = 0;
    let running = true;

    function resize() {
      width = canvas.width = window.innerWidth;
      height = canvas.height = window.innerHeight;
    }

    function spawn() {
      dots.length = 0;
      const count = Math.min(70, Math.floor((width * height) / 28000));
      for (let i = 0; i < count; i += 1) {
        dots.push({
          x: Math.random() * width,
          y: Math.random() * height,
          r: Math.random() * 1.8 + 0.4,
          s: Math.random() * 0.35 + 0.08,
          a: Math.random() * 0.35 + 0.08,
          drift: (Math.random() - 0.5) * 0.25,
        });
      }
    }

    function tick() {
      if (!running) return;
      ctx.clearRect(0, 0, width, height);
      dots.forEach(function (dot) {
        dot.y -= dot.s;
        dot.x += dot.drift;
        if (dot.y < -4) {
          dot.y = height + 4;
          dot.x = Math.random() * width;
        }
        ctx.beginPath();
        ctx.fillStyle = "rgba(56, 214, 255," + dot.a + ")";
        ctx.arc(dot.x, dot.y, dot.r, 0, Math.PI * 2);
        ctx.fill();
      });
      requestAnimationFrame(tick);
    }

    resize();
    spawn();
    tick();
    window.addEventListener("resize", function () {
      resize();
      spawn();
    });
    document.addEventListener("visibilitychange", function () {
      running = document.visibilityState !== "hidden";
      if (running) tick();
    });
  }

  function tiltCards() {
    if (reduced) return;
    document.querySelectorAll(".today-action:not(.today-statement), .quick-card").forEach(function (card) {
      card.addEventListener("pointermove", function (event) {
        const rect = card.getBoundingClientRect();
        const x = (event.clientX - rect.left) / rect.width - 0.5;
        const y = (event.clientY - rect.top) / rect.height - 0.5;
        card.style.transform = "perspective(700px) rotateY(" + (x * -8) + "deg) rotateX(" + (y * 8) + "deg) translateY(-6px)";
      });
      card.addEventListener("pointerleave", function () {
        card.style.transform = "";
      });
    });
  }

  function parseGrouped(text) {
    const raw = (text || "").toString().trim();
    if (!raw) return null;
    const normalized = raw.replace(/\./g, "").replace(",", ".");
    const value = Number(normalized);
    return Number.isFinite(value) ? { value: value, source: raw } : null;
  }

  function countUp() {
    if (reduced) return;
    document.querySelectorAll(".today-stat .value").forEach(function (node) {
      const parsed = parseGrouped(node.textContent);
      if (!parsed) return;
      const duration = 1100;
      const start = performance.now();
      function frame(now) {
        const t = Math.min((now - start) / duration, 1);
        const eased = 1 - Math.pow(1 - t, 3);
        const current = parsed.value * eased;
        if (parsed.source.indexOf(",") >= 0) {
          node.textContent = current.toLocaleString("ar-EG", { maximumFractionDigits: 2 });
        } else {
          node.textContent = Math.round(current).toLocaleString("ar-EG");
        }
        if (t < 1) requestAnimationFrame(frame);
        else node.textContent = parsed.source;
      }
      requestAnimationFrame(frame);
    });
  }

  runParticles();
  tiltCards();
  countUp();
})();
