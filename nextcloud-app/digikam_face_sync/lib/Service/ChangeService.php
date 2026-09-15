<?php

declare(strict_types=1);
namespace OCA\DigikamFaceSync\Service;

use OCA\DigikamFaceSync\Db\ChangeRepository;

/**
 * What Face Sync polls between syncs: has anything changed, and is it a good
 * moment to start.
 */
final class ChangeService {
	public function __construct(private ChangeRepository $repository) {
	}

	/** @return array<string, mixed> */
	public function fingerprint(string $userId): array {
		if (!$this->repository->isAvailable()) {
			throw new \RuntimeException('Recognize face tables are not available');
		}
		return [
			'detections' => $this->repository->detectionSummary($userId),
			'clusters' => $this->repository->clusterSummary($userId),
		];
	}

	/** @return array<string, mixed> */
	public function status(): array {
		$now = time();
		$jobs = $this->repository->runningRecognizeJobs($now);
		$since = null;
		foreach ($jobs as $job) {
			if ($since === null || $job['reserved_at'] < $since) {
				$since = $job['reserved_at'];
			}
		}
		return [
			'recognize_busy' => $jobs !== [],
			'jobs' => array_values(array_unique(array_map(
				static fn (array $job): string => substr(strrchr($job['class'], '\\') ?: $job['class'], 1),
				$jobs,
			))),
			'since' => $since === null ? null : gmdate('c', $since),
		];
	}
}
