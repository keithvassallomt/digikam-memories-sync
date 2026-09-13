<?php

declare(strict_types=1);
namespace OCA\DigikamFaceSync\Service;

use OCA\DigikamFaceSync\Db\RecognizeRepository;
use OCP\Files\File;
use OCP\Files\IRootFolder;

final class FaceExportService {
	public const PAGE_SIZE_MAX = 1000;

	public function __construct(
		private IRootFolder $rootFolder,
		private RecognizeRepository $repository,
	) {
	}

	/** @return list<string> */
	public function people(string $userId): array {
		if (!$this->repository->isAvailable()) {
			throw new \RuntimeException('Recognize face database tables are missing');
		}
		return $this->repository->findPeople($userId);
	}

	/** @return array{detections: list<array<string, mixed>>, nextAfter: int|null} */
	public function list(string $userId, ?string $person, int $after, int $limit): array {
		if (!$this->repository->isAvailable()) {
			throw new \RuntimeException('Recognize face database tables are missing');
		}
		$person = $person === null || trim($person) === ''
			? null
			: FaceImportService::validatePerson($person);
		$after = max(0, $after);
		$limit = max(1, min(self::PAGE_SIZE_MAX, $limit));
		$rows = $this->repository->findNamedDetections($userId, $person, $after, $limit + 1);
		$hasMore = count($rows) > $limit;
		if ($hasMore) {
			array_pop($rows);
		}

		$userFolder = $this->rootFolder->getUserFolder($userId);
		$detections = [];
		$lastId = $after;
		$fileCache = [];
		foreach ($rows as $row) {
			$lastId = (int)$row['id'];
			$fileId = (int)$row['file_id'];
			if (!array_key_exists($fileId, $fileCache)) {
				$node = $userFolder->getFirstNodeById($fileId);
				$relativePath = $node instanceof File
					? $userFolder->getRelativePath($node->getPath())
					: null;
				$fileCache[$fileId] = $node instanceof File
					&& str_starts_with($node->getMimeType(), 'image/')
					&& is_string($relativePath)
					&& $relativePath !== ''
					? [ltrim($relativePath, '/'), $node->getName()]
					: null;
			}
			$file = $fileCache[$fileId];
			if ($file === null) {
				continue;
			}
			$detections[] = [
				'id' => (int)$row['id'],
				'fileId' => $fileId,
				'path' => $file[0],
				'name' => $file[1],
				'person' => (string)$row['title'],
				'x' => (float)$row['x'],
				'y' => (float)$row['y'],
				'width' => (float)$row['width'],
				'height' => (float)$row['height'],
				'clusterId' => (int)$row['cluster_id'],
				'threshold' => (float)($row['threshold'] ?? 0.0),
			];
		}

		return [
			'detections' => $detections,
			'nextAfter' => $hasMore ? $lastId : null,
		];
	}
}
