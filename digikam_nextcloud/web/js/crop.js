// Face-box geometry, shared by the two review screens.

/**
 * Choose a crop that frames every given rectangle, padded and matched to the
 * canvas shape, so a face in a large photo is actually visible.
 */
export function faceCrop(rectangles, imageWidth, imageHeight, targetAspect = 4 / 3) {
  const left = Math.min(...rectangles.map((rect) => rect[0]));
  const top = Math.min(...rectangles.map((rect) => rect[1]));
  const right = Math.max(...rectangles.map((rect) => rect[0] + rect[2]));
  const bottom = Math.max(...rectangles.map((rect) => rect[1] + rect[3]));
  const centreX = (left + right) / 2;
  const centreY = (top + bottom) / 2;
  const faceWidth = Math.max(0.01, right - left);
  const faceHeight = Math.max(0.01, bottom - top);
  const padding = Math.max(0.04, Math.max(faceWidth, faceHeight) * 0.7);
  let width = Math.max(0.18, faceWidth + 2 * padding);
  let height = Math.max(0.135, faceHeight + 2 * padding);
  const normalizedAspect = (targetAspect * imageHeight) / imageWidth;
  if (width / height < normalizedAspect) width = height * normalizedAspect;
  else height = width / normalizedAspect;
  if (width > 1) {
    width = 1;
    height = 1 / normalizedAspect;
  }
  if (height > 1) {
    height = 1;
    width = normalizedAspect;
  }
  const x = Math.max(0, Math.min(1 - width, centreX - width / 2));
  const y = Math.max(0, Math.min(1 - height, centreY - height / 2));
  return { x, y, width, height };
}

/** Position a face box over the cropped canvas, in percentages. */
export function placeBox(element, rect, crop) {
  element.style.left = `${(100 * (rect[0] - crop.x)) / crop.width}%`;
  element.style.top = `${(100 * (rect[1] - crop.y)) / crop.height}%`;
  element.style.width = `${(100 * rect[2]) / crop.width}%`;
  element.style.height = `${(100 * rect[3]) / crop.height}%`;
  element.hidden = false;
}

/** Draw the chosen crop of a loaded image onto a canvas. */
export function drawCrop(canvas, image, crop) {
  const context = canvas.getContext('2d');
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.drawImage(
    image,
    crop.x * image.naturalWidth,
    crop.y * image.naturalHeight,
    crop.width * image.naturalWidth,
    crop.height * image.naturalHeight,
    0,
    0,
    canvas.width,
    canvas.height,
  );
}

/**
 * Move or resize a rectangle from a pointer drag, kept inside the crop.
 * Returns a new rectangle; the caller owns the state.
 */
export function dragRect(rect, crop, corner, dx, dy) {
  const minimum = 0.01;
  let [left, top, width, height] = rect;
  let right = left + width;
  let bottom = top + height;
  const cropRight = crop.x + crop.width;
  const cropBottom = crop.y + crop.height;
  if (corner === 'move') {
    left = Math.max(crop.x, Math.min(cropRight - width, left + dx));
    top = Math.max(crop.y, Math.min(cropBottom - height, top + dy));
    right = left + width;
    bottom = top + height;
  } else {
    if (corner.includes('left')) left = Math.max(crop.x, Math.min(right - minimum, left + dx));
    if (corner.includes('right')) right = Math.min(cropRight, Math.max(left + minimum, right + dx));
    if (corner.includes('top')) top = Math.max(crop.y, Math.min(bottom - minimum, top + dy));
    if (corner.includes('bottom')) bottom = Math.min(cropBottom, Math.max(top + minimum, bottom + dy));
  }
  return [left, top, right - left, bottom - top];
}
