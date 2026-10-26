const TABLE_VERTICAL_RADII = {
  compact: { 2: 29, 4: 29, 5: 33, 6: 29, 7: 33, 8: 29, 9: 31.5 },
  portrait: { 5: 26, 8: 27, 9: 34 },
  desktop: { 2: 32, 4: 32, 5: 40, 6: 32, 7: 34, 8: 32, 9: 34 },
};

function seatAngle(position, count, viewerSeat) {
  // The engine advances through increasing physical seat numbers. Place those
  // numbers clockwise around the table from the viewer's seat at the bottom.
  return Math.PI + 2 * Math.PI * ((position - viewerSeat + count) % count) / count;
}

export function positionAtTable(element, position, count, viewerSeat = 0) {
  const portrait = window.matchMedia('(max-width: 900px)').matches;
  const compact = window.matchMedia('(max-width: 560px)').matches;
  const relativePosition = (position - viewerSeat + count) % count;
  const angle = seatAngle(position, count, viewerSeat);
  const radiusX = compact ? 40.25 : 39;
  const radii = compact ? TABLE_VERTICAL_RADII.compact
    : portrait ? TABLE_VERTICAL_RADII.portrait : TABLE_VERTICAL_RADII.desktop;
  const radiusY = radii[count] ?? (portrait ? 22 : 24);
  const upperDiagonal = count === 8 && (relativePosition === 3 || relativePosition === 5);
  let seatX = 50 + radiusX * Math.sin(angle) +
    (upperDiagonal ? Math.sign(Math.sin(angle)) * (compact ? 7 : 4) : 0);
  if (!portrait && count === 9 && (relativePosition === 1 || relativePosition === 8))
    seatX -= Math.sign(Math.sin(angle)) * 5.5;
  let seatY = relativePosition === 0 ? (compact ? 67 : 65)
    : (portrait ? 46 : 45) - radiusY * Math.cos(angle);
  if (!portrait && count >= 8 && (relativePosition === 1 || relativePosition === count - 1))
    seatY -= count === 9 ? 3.2 : 2;
  element.style.left = `${seatX}%`;
  element.style.top = `${seatY}%`;
}

export function markerSide(position, count, viewerSeat) {
  const angle = seatAngle(position, count, viewerSeat);
  if (Math.abs(Math.sin(angle)) > .78) {
    const side = Math.sin(angle) > 0 ? 'left' : 'right';
    return window.matchMedia('(max-width: 360px)').matches && (count === 3 || count === 7)
      ? `${side}-high` : side;
  }
  return Math.cos(angle) > 0 ? 'bottom' : 'top';
}
