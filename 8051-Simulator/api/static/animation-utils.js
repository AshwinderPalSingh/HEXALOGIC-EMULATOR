export function rafThrottle(callback) {
    let frame = 0;
    let lastArgs = [];

    return (...args) => {
        lastArgs = args;
        if (frame) {
            return;
        }
        frame = window.requestAnimationFrame(() => {
            frame = 0;
            callback(...lastArgs);
        });
    };
}

export function pulseClass(node, className) {
    if (!node) {
        return;
    }
    const key = `pulseTimer${className}`;
    if (node[key]) {
        window.clearTimeout(node[key]);
    }
    node.classList.remove(className);
    window.requestAnimationFrame(() => {
        node.classList.add(className);
        node[key] = window.setTimeout(() => {
            node.classList.remove(className);
            node[key] = 0;
        }, 220);
    });
}

export function animateLoaderProgress(node, value) {
    if (!node) {
        return;
    }
    node.style.width = `${Math.max(6, Math.min(100, Number(value) || 0))}%`;
}
