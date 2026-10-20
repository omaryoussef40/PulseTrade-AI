"""Keep the fixed status bar aligned with Streamlit's changing main area."""

STATUS_DOCK_SCRIPT = r"""
<script>
(() => {
    const previous = window.__pulseTradeStatusDock;
    if (previous) previous.dispose();
    const main = document.querySelector('[data-testid="stMain"]');
    const sidebar = document.querySelector('[data-testid="stSidebar"]');
    const dock = document.querySelector('.fixed-status-dock');
    if (!dock) return;
    let frame = null;
    const update = () => {
        frame = null;
        if (!dock.isConnected) return;
        const viewport = document.documentElement.clientWidth;
        const area = main ? main.getBoundingClientRect() : {left: 0, right: viewport};
        const menu = sidebar ? sidebar.getBoundingClientRect() : null;
        const expanded = sidebar && sidebar.getAttribute('aria-expanded') !== 'false';
        const menuEdge = expanded && menu && menu.width > 0 ? menu.right : 0;
        const left = Math.min(viewport, Math.max(0, area.left, menuEdge));
        const right = Math.max(0, viewport - area.right);
        document.documentElement.style.setProperty('--pt-status-left', `${left}px`);
        document.documentElement.style.setProperty('--pt-status-right', `${right}px`);
        document.documentElement.style.setProperty('--pt-status-height', `${dock.getBoundingClientRect().height}px`);
    };
    const schedule = () => {
        if (frame === null) frame = requestAnimationFrame(update);
    };
    const resize = new ResizeObserver(schedule);
    [main, sidebar, dock].filter(Boolean).forEach(element => resize.observe(element));
    const mutation = new MutationObserver(schedule);
    if (sidebar) mutation.observe(sidebar, {
        attributes: true, attributeFilter: ['aria-expanded', 'style', 'class']
    });
    window.addEventListener('resize', schedule);
    document.addEventListener('transitionend', schedule, true);
    window.__pulseTradeStatusDock = {
        dispose() {
            resize.disconnect();
            mutation.disconnect();
            window.removeEventListener('resize', schedule);
            document.removeEventListener('transitionend', schedule, true);
            if (frame !== null) cancelAnimationFrame(frame);
        }
    };
    update();
})();
</script>
"""
