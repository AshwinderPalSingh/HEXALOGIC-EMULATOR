export const PANEL_LAYOUT_KEY = "hexlogic-layout-v6";
export const MIN_PANEL_HEIGHT = 140;
export const MIN_LEFT_WIDTH = 240;
export const MIN_RIGHT_WIDTH = 300;
export const MIN_CENTER_WIDTH = 420;
export const COLUMN_GUTTER_ALLOWANCE = 16;
export const MIN_FLOAT_WIDTH = 260;
export const MIN_FLOAT_HEIGHT = 180;
export const SNAP_EDGE_THRESHOLD = 132;

export function clampValue(value, min, max) {
    return Math.max(min, Math.min(max, value));
}

export function resolveVerticalSplit(previousHeight, nextHeight, delta, minHeight = MIN_PANEL_HEIGHT) {
    let nextPrevious = previousHeight + delta;
    let nextNext = nextHeight - delta;
    const total = previousHeight + nextHeight;

    if (nextPrevious < minHeight) {
        nextPrevious = minHeight;
        nextNext = total - nextPrevious;
    }
    if (nextNext < minHeight) {
        nextNext = minHeight;
        nextPrevious = total - nextNext;
    }
    return { previous: nextPrevious, next: nextNext };
}

export function resolveLeftColumnWidth({
    leftStart,
    delta,
    workspaceWidth,
    rightWidth,
    minLeft = MIN_LEFT_WIDTH,
    minCenter = MIN_CENTER_WIDTH,
    gutterAllowance = COLUMN_GUTTER_ALLOWANCE,
}) {
    const maxLeft = workspaceWidth - minCenter - rightWidth - gutterAllowance;
    return clampValue(leftStart + delta, minLeft, maxLeft);
}

export function resolveRightColumnWidth({
    rightStart,
    delta,
    workspaceWidth,
    leftWidth,
    minRight = MIN_RIGHT_WIDTH,
    minCenter = MIN_CENTER_WIDTH,
    gutterAllowance = COLUMN_GUTTER_ALLOWANCE,
}) {
    const maxRight = workspaceWidth - minCenter - leftWidth - gutterAllowance;
    return clampValue(rightStart - delta, minRight, maxRight);
}

export function clampFloatingBounds({
    left,
    top,
    width,
    height,
    workspaceWidth,
    workspaceHeight,
    minWidth = MIN_FLOAT_WIDTH,
    minHeight = MIN_FLOAT_HEIGHT,
}) {
    const nextWidth = clampValue(width, minWidth, Math.max(minWidth, workspaceWidth - 24));
    const nextHeight = clampValue(height, minHeight, Math.max(minHeight, workspaceHeight - 24));
    return {
        left: clampValue(left, 0, Math.max(0, workspaceWidth - nextWidth)),
        top: clampValue(top, 0, Math.max(0, workspaceHeight - nextHeight)),
        width: nextWidth,
        height: nextHeight,
    };
}

export function detectSnapZone(pointerX, pointerY, workspaceRect, threshold = SNAP_EDGE_THRESHOLD) {
    const localX = pointerX - workspaceRect.left;
    const localY = pointerY - workspaceRect.top;
    if (localX < 0 || localY < 0 || localX > workspaceRect.width || localY > workspaceRect.height) {
        return null;
    }

    const edge = Math.min(
        threshold,
        Math.floor(Math.min(workspaceRect.width, workspaceRect.height) * 0.18),
    );

    if (localX <= edge) {
        return "left";
    }
    if (localX >= workspaceRect.width - edge) {
        return "right";
    }
    if (localY <= edge) {
        return "top";
    }
    if (localY >= workspaceRect.height - edge) {
        return "bottom";
    }
    return "center";
}
