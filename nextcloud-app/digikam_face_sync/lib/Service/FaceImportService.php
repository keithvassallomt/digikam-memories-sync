<?php

declare(strict_types=1);
namespace OCA\DigikamFaceSync\Service;

use OCA\DigikamFaceSync\Db\RecognizeRepository;
use OCP\Files\File;
use OCP\Files\IRootFolder;

final class FaceImportService {
	public const DUPLICATE_IOU = 0.4;

	public function __construct(
		private IRootFolder $rootFolder,
		private RecognizeRepository $repository,
		private FaceDescriptorExtractor $descriptorExtractor,
	) {
	}

	/** @return array<string, mixed> */
	public function capabilities(): array {
		$compatibility = $this->descriptorExtractor->compatibility();
		if ($compatibility['available'] && !$this->repository->isAvailable()) {
			$compatibility['available'] = false;
			$compatibility['reason'] = 'Recognize face database tables are missing';
		}
		return [
			'apiVersion' => 5,
			'createFaceDetection' => $compatibility['available'],
			'confirmedFaceImport' => $compatibility['available'],
			'assignFaceDetection' => $this->repository->isAvailable(),
			'listFaceDetections' => $this->repository->isAvailable(),
			'listPeople' => $this->repository->isAvailable(),
			'changeFingerprint' => $this->repository->isAvailable(),
			'recognizeStatus' => $this->repository->isAvailable(),
			'coordinates' => 'relative',
			'faceVector' => 'generated-by-recognize',
			'recognizeVersion' => $compatibility['recognizeVersion'],
			'reason' => $compatibility['reason'],
		];
	}

	/** @return array{changed: bool, detection: array<string, mixed>} */
	public function assign(
		string $userId,
		int $fileId,
		int $detectionId,
		string $person,
	): array {
		if (!$this->repository->isAvailable()) {
			throw new \RuntimeException('Recognize face database tables are missing');
		}
		$person = self::validatePerson($person);
		$node = $this->rootFolder->getUserFolder($userId)->getFirstNodeById($fileId);
		if (!$node instanceof File || !str_starts_with($node->getMimeType(), 'image/')) {
			throw new \OutOfBoundsException('Image was not found or is not accessible');
		}
		$detection = $this->repository->findDetection($detectionId, $fileId, $userId);
		if ($detection === null) {
			throw new \OutOfBoundsException('Face detection was not found for this image');
		}

		$clusterId = $this->repository->getOrCreateCluster($userId, $person);
		$changed = (int)($detection['cluster_id'] ?? 0) !== $clusterId;
		if ($changed) {
			$this->repository->assignDetectionCluster($detectionId, $fileId, $userId, $clusterId);
			$detection['cluster_id'] = $clusterId;
		}
		return ['changed' => $changed, 'detection' => self::detectionResponse($detection)];
	}

	/** @return array{created: bool, detection: array<string, mixed>, score: float|null} */
	public function import(
		string $userId,
		int $fileId,
		string $person,
		float $x,
		float $y,
		float $width,
		float $height,
		bool $confirmed = false,
	): array {
		$capabilities = $this->capabilities();
		if (!$capabilities['createFaceDetection']) {
			throw new \RuntimeException((string)($capabilities['reason'] ?? 'Face import is unavailable'));
		}
		$person = self::validatePerson($person);
		self::validateRectangle($x, $y, $width, $height);

		$node = $this->rootFolder->getUserFolder($userId)->getFirstNodeById($fileId);
		if (!$node instanceof File || !str_starts_with($node->getMimeType(), 'image/')) {
			throw new \OutOfBoundsException('Image was not found or is not accessible');
		}

		foreach ($this->repository->findDetections($fileId, $userId) as $existing) {
			if (self::iou($x, $y, $width, $height, $existing) < self::DUPLICATE_IOU) {
				continue;
			}
			if ($this->detectionMatchesPerson($existing, $userId, $person)) {
				return [
					'created' => false,
					'detection' => self::detectionResponse($existing),
					'score' => null,
				];
			}
			throw new \DomainException('An overlapping face detection already belongs to another person');
		}

		$descriptor = $this->descriptorExtractor->extract($node, $x, $y, $width, $height, $confirmed);
		$clusterId = $this->repository->getOrCreateCluster($userId, $person);
		$detectionId = $this->repository->insertDetection(
			$userId,
			$fileId,
			$x,
			$y,
			$width,
			$height,
			$descriptor['vector'],
			$clusterId,
		);

		return [
			'created' => true,
			'detection' => [
				'id' => $detectionId,
				'userId' => $userId,
				'fileId' => $fileId,
				'x' => $x,
				'y' => $y,
				'width' => $width,
				'height' => $height,
				'clusterId' => $clusterId,
				'threshold' => 0.0,
			],
			'score' => $descriptor['score'],
		];
	}

	public static function validatePerson(string $person): string {
		$person = trim($person);
		if ($person === '' || strlen($person) > 255) {
			throw new \InvalidArgumentException('Person must contain between 1 and 255 characters');
		}
		if (preg_match('/[\/\\\\\x00-\x1F\x7F]/u', $person) === 1 || preg_match('/^[0-9]+$/', $person) === 1) {
			throw new \InvalidArgumentException('Person contains characters that Recognize does not allow');
		}
		return $person;
	}

	public static function validateRectangle(float $x, float $y, float $width, float $height): void {
		foreach ([$x, $y, $width, $height] as $value) {
			if (!is_finite($value)) {
				throw new \InvalidArgumentException('Face rectangle must contain finite numbers');
			}
		}
		if ($x < 0.0 || $y < 0.0 || $width <= 0.0 || $height <= 0.0
			|| $x + $width > 1.000001 || $y + $height > 1.000001) {
			throw new \InvalidArgumentException('Face rectangle must fit within normalized image coordinates');
		}
	}

	/** @param array<string, mixed> $detection */
	private function detectionMatchesPerson(array $detection, string $userId, string $person): bool {
		$clusterId = isset($detection['cluster_id']) ? (int)$detection['cluster_id'] : 0;
		if ($clusterId < 1) {
			return false;
		}
		$cluster = $this->repository->findClusterById($clusterId);
		return $cluster !== null
			&& (string)$cluster['user_id'] === $userId
			&& (string)$cluster['title'] === $person;
	}

	/** @param array<string, mixed> $other */
	private static function iou(float $x, float $y, float $width, float $height, array $other): float {
		$otherX = (float)$other['x'];
		$otherY = (float)$other['y'];
		$otherWidth = (float)$other['width'];
		$otherHeight = (float)$other['height'];
		$intersectionWidth = max(0.0, min($x + $width, $otherX + $otherWidth) - max($x, $otherX));
		$intersectionHeight = max(0.0, min($y + $height, $otherY + $otherHeight) - max($y, $otherY));
		$intersection = $intersectionWidth * $intersectionHeight;
		$union = $width * $height + $otherWidth * $otherHeight - $intersection;
		return $union > 0.0 ? $intersection / $union : 0.0;
	}

	/** @param array<string, mixed> $detection
	 * @return array<string, mixed>
	 */
	private static function detectionResponse(array $detection): array {
		return [
			'id' => (int)$detection['id'],
			'userId' => (string)$detection['user_id'],
			'fileId' => (int)$detection['file_id'],
			'x' => (float)$detection['x'],
			'y' => (float)$detection['y'],
			'width' => (float)$detection['width'],
			'height' => (float)$detection['height'],
			'clusterId' => isset($detection['cluster_id']) ? (int)$detection['cluster_id'] : null,
			'threshold' => (float)($detection['threshold'] ?? 0.0),
		];
	}
}
