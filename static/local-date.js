(function updateLocalDates() {
    const now = new Date();
    const weekdayNames = ["日", "一", "二", "三", "四", "五", "六"];
    const year = now.getFullYear();
    const month = now.getMonth() + 1;
    const day = now.getDate();

    document.querySelectorAll("[data-current-local-date]").forEach(element => {
        if (element.dataset.dateFormat === "full") {
            element.textContent =
                `${year}年${month}月${day}日 · 星期${weekdayNames[now.getDay()]}`;
        } else {
            element.textContent = `${month}月${day}日`;
        }
    });
})();
