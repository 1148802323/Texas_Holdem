import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('../src/tableGeometry.js', import.meta.url), 'utf8');
const { markerSide, positionAtTable } = await import(
  `data:text/javascript,${encodeURIComponent(source)}`
);

function seatPoint(position, count, viewerSeat, width) {
  globalThis.window = {
    matchMedia: query => ({ matches: width <= Number(query.match(/\d+/)[0]) }),
  };
  const element = { style: {} };
  positionAtTable(element, position, count, viewerSeat);
  return [parseFloat(element.style.left), parseFloat(element.style.top)];
}

test('increasing seat numbers run clockwise from every player viewpoint', () => {
  for (const width of [320, 390, 1280]) {
    for (let count = 3; count <= 9; count++) {
      for (let viewerSeat = 0; viewerSeat < count; viewerSeat++) {
        const [bottomX, bottomY] = seatPoint(viewerSeat, count, viewerSeat, width);
        const [nextX] = seatPoint((viewerSeat + 1) % count, count, viewerSeat, width);
        const [previousX] = seatPoint((viewerSeat + count - 1) % count, count, viewerSeat, width);
        assert.ok(Math.abs(bottomX - 50) < 0.001, `seat ${viewerSeat} is centered`);
        assert.ok(bottomY > 60, `seat ${viewerSeat} is at the bottom`);
        assert.ok(nextX < bottomX, `next seat is clockwise on ${count}-seat table at ${width}px`);
        assert.ok(previousX > bottomX, `previous seat is counterclockwise on ${count}-seat table at ${width}px`);
      }
    }
  }
});

test('dealer and wager markers face the table center after mirroring', () => {
  for (const width of [320, 1280]) {
    globalThis.window = {
      matchMedia: query => ({ matches: width <= Number(query.match(/\d+/)[0]) }),
    };
    assert.equal(markerSide(0, 4, 0), 'top');
    assert.equal(markerSide(1, 4, 0), 'right');
    assert.equal(markerSide(2, 4, 0), 'bottom');
    assert.equal(markerSide(3, 4, 0), 'left');
  }
});
