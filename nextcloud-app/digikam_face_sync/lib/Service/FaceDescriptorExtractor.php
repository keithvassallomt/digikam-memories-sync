<?php

declare(strict_types=1);
namespace OCA\DigikamFaceSync\Service;

use OCP\App\IAppManager;
use OCP\Files\File;
use OCP\Files\NotFoundException;
use OCP\Files\NotPermittedException;
use OCP\IAppConfig;
use OCP\IPreview;
use OCP\ITempManager;
use Psr\Log\LoggerInterface;
use Symfony\Component\Process\Exception\ProcessFailedException;
use Symfony\Component\Process\Exception\ProcessTimedOutException;
use Symfony\Component\Process\Process;

final class FaceDescriptorExtractor {
	private const FACE_VECTOR_SIZE = 128;
	private const PREVIEW_DIMENSION = 1024;
	private const NATIVE_TIMEOUT = 120;
	private const PURE_JS_TIMEOUT = 360;

	public function __construct(
		private IAppManager $appManager,
		private IAppConfig $config,
		private IPreview $previewProvider,
		private ITempManager $tempManager,
		private LoggerInterface $logger,
	) {
	}

	/** @return array{available: bool, recognizeVersion: string, reason: string|null} */
	public function compatibility(): array {
		try {
			$recognizePath = $this->appManager->getAppPath('recognize');
			$version = $this->appManager->getAppVersion('recognize');
		} catch (\Throwable $e) {
			return ['available' => false, 'recognizeVersion' => '', 'reason' => 'Recognize is not installed'];
		}
		// Recognize 13 changed nothing this app depends on: the face tables are
		// untouched between 12.0.2 and 13.1.0, face-api stays on 1.7.x so the
		// model format and API are the same, and node_binary is still where it
		// was. The upper bound stays, because an untested major is exactly what
		// this guard is for.
		if (version_compare($version, '12.0.0', '<') || version_compare($version, '14.0.0', '>=')) {
			return [
				'available' => false,
				'recognizeVersion' => $version,
				'reason' => 'This release supports Recognize 12.x and 13.x',
			];
		}

		$nodeBinary = $this->recognizeConfig('node_binary');
		$modelPath = $recognizePath . '/node_modules/@vladmandic/face-api/model';
		if ($nodeBinary === '') {
			return ['available' => false, 'recognizeVersion' => $version, 'reason' => 'Recognize has no configured Node binary'];
		}
		if (!is_dir($modelPath)) {
			return ['available' => false, 'recognizeVersion' => $version, 'reason' => 'Recognize face model files are missing'];
		}
		return ['available' => true, 'recognizeVersion' => $version, 'reason' => null];
	}

	/** @return array{vector: list<float>, score: float} */
	public function extract(File $file, float $x, float $y, float $width, float $height, bool $confirmed = false): array {
		$compatibility = $this->compatibility();
		if (!$compatibility['available']) {
			throw new \RuntimeException($compatibility['reason'] ?? 'Recognize is unavailable');
		}
		$temporary = null;
		try {
			$recognizePath = $this->appManager->getAppPath('recognize');
			[$imagePath, $temporary] = $this->prepareImage($file);
			$input = json_encode([
				'path' => $imagePath,
				'x' => $x,
				'y' => $y,
				'width' => $width,
				'height' => $height,
				'confirmed' => $confirmed,
			], JSON_THROW_ON_ERROR) . "\n";

			$command = [
				$this->recognizeConfig('node_binary'),
				dirname(__DIR__, 2) . '/src/classifier_faces_region.cjs',
				$recognizePath,
			];
			$niceBinary = trim($this->recognizeConfig('nice_binary'));
			if ($niceBinary !== '') {
				$command = [
					$niceBinary,
					'-' . $this->recognizeConfig('nice_value', '0'),
					...$command,
				];
			}

			$process = new Process($command, __DIR__);
			$environment = [];
			if ($this->recognizeConfig('tensorflow.gpu', 'false') === 'true') {
				$environment['RECOGNIZE_GPU'] = 'true';
			}
			$pureJs = $this->recognizeConfig('tensorflow.purejs', 'false') === 'true';
			if ($pureJs) {
				$environment['RECOGNIZE_PUREJS'] = 'true';
			}
			$cores = $this->recognizeConfig('tensorflow.cores', '0');
			if ($cores !== '0') {
				$environment['RECOGNIZE_CORES'] = $cores;
			}
			$process->setEnv($environment);
			$process->setTimeout($pureJs ? self::PURE_JS_TIMEOUT : self::NATIVE_TIMEOUT);
			$process->setInput($input);
			$process->mustRun();
			$result = $this->parseResult($process->getOutput());
			if (isset($result['error'])) {
				throw new \RuntimeException((string)$result['error']);
			}
			$vector = array_values(array_map('floatval', $result['vector'] ?? []));
			if (count($vector) !== self::FACE_VECTOR_SIZE) {
				throw new \RuntimeException('Recognize returned an invalid face descriptor');
			}
			return ['vector' => $vector, 'score' => (float)($result['score'] ?? 0.0)];
		} catch (ProcessTimedOutException $e) {
			throw new \RuntimeException('Face descriptor extraction timed out', 0, $e);
		} catch (ProcessFailedException $e) {
			$this->logger->warning('Face descriptor process failed', ['exception' => $e]);
			throw new \RuntimeException('Face descriptor extraction failed', 0, $e);
		} finally {
			if ($temporary !== null) {
				@unlink($temporary);
			}
		}
	}

	/** @return array<string, mixed> */
	private function parseResult(string $output): array {
		$lines = array_reverse(preg_split('/\R/', trim($output)) ?: []);
		foreach ($lines as $line) {
			if (trim($line) === '') {
				continue;
			}
			try {
				$decoded = json_decode($line, true, 512, JSON_THROW_ON_ERROR);
				if (is_array($decoded)) {
					return $decoded;
				}
			} catch (\JsonException $e) {
				continue;
			}
		}
		throw new \RuntimeException('Recognize returned no face descriptor result');
	}

	private function recognizeConfig(string $key, string $default = ''): string {
		return $this->config->getValueString('recognize', $key, $default, lazy: true);
	}

	/** @return array{string, string|null} */
	private function prepareImage(File $file): array {
		if ($this->previewProvider->isAvailable($file)) {
			try {
				$path = $this->generatePreview($file);
				return [$path, $path];
			} catch (\Throwable $e) {
				$this->logger->warning('Could not create preview for face import', ['exception' => $e]);
			}
		}
		$path = $file->getStorage()->getLocalFile($file->getInternalPath());
		if (!is_string($path)) {
			throw new NotFoundException('Image has no accessible local representation');
		}
		return [$path, null];
	}

	private function generatePreview(File $file): string {
		$previewFile = $this->previewProvider->getPreview($file, self::PREVIEW_DIMENSION, self::PREVIEW_DIMENSION);
		try {
			$input = $previewFile->read();
		} catch (NotPermittedException $e) {
			throw new \RuntimeException('Could not read image preview', 0, $e);
		}
		$tmp = $this->tempManager->getTemporaryFile('.jpg');
		if ($tmp === false) {
			throw new \RuntimeException('Could not create temporary preview file');
		}
		$output = fopen($tmp, 'wb');
		if ($output === false) {
			fclose($input);
			@unlink($tmp);
			throw new \RuntimeException('Could not open temporary preview file');
		}
		$copied = stream_copy_to_stream($input, $output);
		fclose($input);
		fclose($output);
		if ($copied === false) {
			@unlink($tmp);
			throw new \RuntimeException('Could not copy image preview');
		}

		$imageType = exif_imagetype($tmp);
		if (in_array($imageType, [IMAGETYPE_WEBP, IMAGETYPE_AVIF, false], true)) {
			$contents = file_get_contents($tmp);
			$image = $contents === false ? false : imagecreatefromstring($contents);
			if ($image === false || imagejpeg($image, $tmp, 100) === false) {
				if ($image !== false) {
					imagedestroy($image);
				}
				@unlink($tmp);
				throw new \RuntimeException('Could not convert image preview to JPEG');
			}
			imagedestroy($image);
		}
		return $tmp;
	}
}
