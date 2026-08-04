document.addEventListener("DOMContentLoaded", () => {
  const bindConfirmations = (root = document) => {
    root.querySelectorAll("form[data-confirm]").forEach((form) => {
      if (form.dataset.confirmBound) return;
      form.dataset.confirmBound = "true";
      form.addEventListener("submit", (event) => {
        if (!window.confirm(form.dataset.confirm)) event.preventDefault();
      });
    });
  };

  let sortable = null;
  let isDragging = false;
  let refreshController = null;

  const initializeGrid = () => {
    const grid = document.querySelector("#account-grid");
    if (!grid || !window.Sortable) return;
    if (sortable) sortable.destroy();
    sortable = new Sortable(grid, {
      animation: 180,
      handle: ".drag-handle",
      ghostClass: "drag-ghost",
      onStart: () => {
        isDragging = true;
        if (refreshController) refreshController.abort();
      },
      onEnd: async () => {
        try {
          const ids = [...grid.querySelectorAll("[data-account-id]")].map((card) => Number(card.dataset.accountId));
          const response = await fetch(grid.dataset.reorderUrl, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ids}),
          });
          if (!response.ok) window.location.reload();
        } finally {
          isDragging = false;
        }
      },
    });
  };

  const refreshAccountGrid = async () => {
    const region = document.querySelector("#account-grid-region");
    if (!region || document.hidden || isDragging || refreshController || region.contains(document.activeElement)) return;
    refreshController = new AbortController();
    try {
      const response = await fetch("/", {
        cache: "no-store",
        headers: {"X-Dashboard-Poll": "1"},
        signal: refreshController.signal,
      });
      if (!response.ok) return;
      const nextDocument = new DOMParser().parseFromString(await response.text(), "text/html");
      const nextRegion = nextDocument.querySelector("#account-grid-region");
      if (!nextRegion || isDragging) return;
      if (sortable) {
        sortable.destroy();
        sortable = null;
      }
      region.innerHTML = nextRegion.innerHTML;
      bindConfirmations(region);
      initializeGrid();
    } catch (error) {
      if (error.name !== "AbortError") console.warn("Dashboard refresh failed", error);
    } finally {
      refreshController = null;
    }
  };

  bindConfirmations();
  initializeGrid();
  const region = document.querySelector("#account-grid-region");
  if (region) {
    const interval = Number(region.dataset.refreshInterval) || 5000;
    window.setInterval(refreshAccountGrid, interval);
  }

  const canvas = document.querySelector("#stats-chart");
  if (canvas && window.Chart) {
    const data = JSON.parse(canvas.dataset.chart);
    new Chart(canvas, {
      type: "line",
      data: {
        labels: data.labels,
        datasets: [
          {label: "粉絲", data: data.followers, borderColor: "#7c5cff", backgroundColor: "#7c5cff22", tension: 0.3, spanGaps: true},
          {label: "追蹤中", data: data.following, borderColor: "#2dd4bf", backgroundColor: "#2dd4bf22", tension: 0.3, spanGaps: true},
        ],
      },
      options: {responsive: true, plugins: {legend: {labels: {color: "#b8b5c8"}}}, scales: {x: {ticks: {color: "#777487", maxTicksLimit: 8}, grid: {color: "#292733"}}, y: {ticks: {color: "#777487"}, grid: {color: "#292733"}}}},
    });
  }
});
