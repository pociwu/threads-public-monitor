document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  const grid = document.querySelector("#account-grid");
  if (grid && window.Sortable) {
    new Sortable(grid, {
      animation: 180,
      handle: ".drag-handle",
      ghostClass: "drag-ghost",
      onEnd: async () => {
        const ids = [...grid.querySelectorAll("[data-account-id]")].map((card) => Number(card.dataset.accountId));
        const response = await fetch(grid.dataset.reorderUrl, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ids}),
        });
        if (!response.ok) window.location.reload();
      },
    });
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

