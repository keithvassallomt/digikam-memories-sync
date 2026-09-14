const path = require('path')
const fs = require('fs/promises')

if (process.argv.length !== 3) throw new Error('Usage: node classifier_faces_region.js RECOGNIZE_APP_PATH')

const recognizeRoot = path.resolve(process.argv[2])
const fromRecognize = modulePath => require(path.join(recognizeRoot, 'node_modules', modulePath))

let tf, faceapi, Jimp
let PUREJS = false
if (process.env.RECOGNIZE_PUREJS === 'true') {
	tf = fromRecognize('@tensorflow/tfjs')
	fromRecognize('@tensorflow/tfjs-backend-wasm')
	faceapi = require(path.join(recognizeRoot, 'node_modules/@vladmandic/face-api/dist/face-api.node-wasm.js'))
	Jimp = fromRecognize('jimp')
	PUREJS = true
} else {
	try {
		if (process.env.RECOGNIZE_GPU === 'true') {
			tf = fromRecognize('@tensorflow/tfjs-node-gpu')
			faceapi = require(path.join(recognizeRoot, 'node_modules/@vladmandic/face-api/dist/face-api.node-gpu.js'))
		} else {
			tf = fromRecognize('@tensorflow/tfjs-node')
			faceapi = require(path.join(recognizeRoot, 'node_modules/@vladmandic/face-api/dist/face-api.node.js'))
		}
	} catch (e) {
		console.error(e)
		console.error('Trying js-only mode')
		tf = fromRecognize('@tensorflow/tfjs')
		fromRecognize('@tensorflow/tfjs-backend-wasm')
		faceapi = require(path.join(recognizeRoot, 'node_modules/@vladmandic/face-api/dist/face-api.node-wasm.js'))
		Jimp = fromRecognize('jimp')
		PUREJS = true
	}
}

async function readStdin() {
	let input = ''
	for await (const chunk of process.stdin) input += chunk
	return input
}

async function main() {
	await faceapi.nets.ssdMobilenetv1.loadFromDisk(path.join(recognizeRoot, 'node_modules/@vladmandic/face-api/model'))
	await faceapi.nets.faceLandmark68Net.loadFromDisk(path.join(recognizeRoot, 'node_modules/@vladmandic/face-api/model'))
	await faceapi.nets.faceRecognitionNet.loadFromDisk(path.join(recognizeRoot, 'node_modules/@vladmandic/face-api/model'))

	const lines = (await readStdin()).split('\n').filter(line => line.trim() !== '')
	for (const line of lines) {
		let tensor
		try {
			const request = JSON.parse(line)
			if (PUREJS) {
				tensor = await createTensor(await Jimp.read(request.path))
			} else {
				tensor = await tf.node.decodeImage(await fs.readFile(request.path), 3)
			}
			const match = await descriptorForRegion(tensor, request)
			console.log(JSON.stringify(match || { error: 'No face found inside the supplied rectangle' }))
		} catch (e) {
			console.error(e)
			console.log(JSON.stringify({ error: e.message || String(e) }))
		} finally {
			if (tensor) tensor.dispose()
		}
	}
}

async function descriptorForRegion(tensor, region) {
	const imageHeight = tensor.shape[0]
	const imageWidth = tensor.shape[1]
	const paddings = [0.35, 0.15, 0, -0.1, 0.6]
	const options = new faceapi.SsdMobilenetv1Options({ minConfidence: 0.05, maxResults: 10 })
	let best = null

	for (const padding of paddings) {
		const padX = region.width * padding
		const padY = region.height * padding
		const x1 = Math.max(0, region.x - padX)
		const y1 = Math.max(0, region.y - padY)
		const x2 = Math.min(1, region.x + region.width + padX)
		const y2 = Math.min(1, region.y + region.height + padY)
		const left = Math.floor(x1 * imageWidth)
		const top = Math.floor(y1 * imageHeight)
		const width = Math.max(1, Math.ceil((x2 - x1) * imageWidth))
		const height = Math.max(1, Math.ceil((y2 - y1) * imageHeight))
		const crop = tf.tidy(() => {
			const sliced = tf.slice(tensor, [top, left, 0], [Math.min(height, imageHeight - top), Math.min(width, imageWidth - left), 3])
			return sliced.clone()
		})

		try {
			const target = {
				x: (region.x - x1) / (x2 - x1),
				y: (region.y - y1) / (y2 - y1),
				width: region.width / (x2 - x1),
				height: region.height / (y2 - y1),
			}
			const results = await faceapi.detectAllFaces(crop, options).withFaceLandmarks().withFaceDescriptors()
			const candidates = results
				.map(result => {
					const overlap = overlapFraction(result.detection.relativeBox, target)
					return { result, overlap, quality: overlap * result.detection.score }
				})
				.filter(candidate => candidate.overlap > 0.25)
				.sort((a, b) => (b.quality - a.quality) || (b.overlap - a.overlap))
			if (candidates.length > 0) {
				if (best === null || candidates[0].quality > best.quality) best = candidates[0]
				if (best.result.detection.score >= 0.8 && best.overlap >= 0.7) break
			}
		} finally {
			crop.dispose()
		}
	}
	if (best !== null) {
		return {
			vector: Array.from(best.result.descriptor),
			score: best.result.detection.score,
			method: 'detected',
		}
	}
	if (region.confirmed !== true) return null
	return descriptorForConfirmedRegion(tensor, region)
}

async function descriptorForConfirmedRegion(tensor, region) {
	const imageHeight = tensor.shape[0]
	const imageWidth = tensor.shape[1]
	const left = Math.max(0, Math.floor(region.x * imageWidth))
	const top = Math.max(0, Math.floor(region.y * imageHeight))
	const width = Math.max(1, Math.min(imageWidth - left, Math.ceil(region.width * imageWidth)))
	const height = Math.max(1, Math.min(imageHeight - top, Math.ceil(region.height * imageHeight)))
	const face = tf.tidy(() => tf.slice(tensor, [top, left, 0], [height, width, 3]).clone())
	let alignedFaces = []
	try {
		// A reviewed digiKam box is already the face detection. Run the same
		// landmark alignment and recognition model that Recognize uses after SSD.
		const landmarks = await faceapi.nets.faceLandmark68Net.detectLandmarks(face)
		const alignedBox = landmarks.align(null, { useDlibAlignment: true })
		alignedFaces = await faceapi.extractFaceTensors(face, [alignedBox])
		if (alignedFaces.length !== 1) return null
		const descriptor = await faceapi.nets.faceRecognitionNet.computeFaceDescriptor(alignedFaces[0])
		const vector = Array.from(descriptor)
		if (vector.length !== 128 || vector.some(value => !Number.isFinite(value))) return null
		return { vector, score: 0, method: 'confirmed-region' }
	} finally {
		face.dispose()
		alignedFaces.forEach(aligned => aligned.dispose())
	}
}

function overlapFraction(detected, target) {
	const x1 = Math.max(detected.x, target.x)
	const y1 = Math.max(detected.y, target.y)
	const x2 = Math.min(detected.x + detected.width, target.x + target.width)
	const y2 = Math.min(detected.y + detected.height, target.y + target.height)
	const intersection = Math.max(0, x2 - x1) * Math.max(0, y2 - y1)
	const detectedArea = Math.max(0, detected.width) * Math.max(0, detected.height)
	return detectedArea > 0 ? intersection / detectedArea : 0
}

async function createTensor(image) {
	const channels = 3
	const values = new Float32Array(image.bitmap.width * image.bitmap.height * channels)
	let i = 0
	image.scan(0, 0, image.bitmap.width, image.bitmap.height, (x, y) => {
		const pixel = Jimp.intToRGBA(image.getPixelColor(x, y))
		values[i * channels] = pixel.r
		values[i * channels + 1] = pixel.g
		values[i * channels + 2] = pixel.b
		i++
	})
	return tf.tensor3d(values, [image.bitmap.height, image.bitmap.width, channels], 'float32')
}

tf.setBackend(PUREJS ? 'wasm' : 'tensorflow')
	.then(() => main())
	.catch(error => {
		console.error(error)
		process.exitCode = 1
	})
