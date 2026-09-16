<?php

declare(strict_types=1);
namespace OCA\DigikamFaceSync\Db;

use OCP\DB\QueryBuilder\IQueryBuilder;
use OCP\IDBConnection;

/**
 * Cheap answers to "has anything moved?" and "is Recognize busy?".
 *
 * Face Sync polls these, so they must stay aggregate queries. Reading every
 * detection on every poll would cost more than the sync it is trying to avoid.
 */
final class ChangeRepository {
	private const DETECTIONS_TABLE = 'recognize_face_detections';
	private const CLUSTERS_TABLE = 'recognize_face_clusters';
	private const JOBS_TABLE = 'jobs';

	/** Recognize's background jobs all live under this namespace. */
	private const RECOGNIZE_JOB_PREFIX = 'OCA\\Recognize\\BackgroundJobs\\';

	/** Nextcloud releases a reservation this old, so ignore anything older. */
	private const STALE_RESERVATION_SECONDS = 43200;

	public function __construct(private IDBConnection $db) {
	}

	public function isAvailable(): bool {
		return $this->db->tableExists(self::DETECTIONS_TABLE)
			&& $this->db->tableExists(self::CLUSTERS_TABLE);
	}

	/**
	 * A fingerprint of every detection that belongs to a named person.
	 *
	 * The hash covers which person each face is assigned to, so a new face, a
	 * reassignment and a swap between two people all change it. A second hash
	 * is kept per person, which is what lets Face Sync look at one person
	 * rather than the whole library when only one of them moved. The same
	 * scan produces both, so polling costs what it always did.
	 *
	 * @return array{count: int, checksum: string, people: array<string, string>}
	 */
	public function detectionSummary(string $userId): array {
		$qb = $this->db->getQueryBuilder();
		$qb->select('d.id', 'd.cluster_id', 'c.title')
			->from(self::DETECTIONS_TABLE, 'd')
			->innerJoin('d', self::CLUSTERS_TABLE, 'c', $qb->expr()->eq('d.cluster_id', 'c.id'))
			->where($qb->expr()->eq('d.user_id', $qb->createNamedParameter($userId, IQueryBuilder::PARAM_STR)))
			->andWhere($qb->expr()->neq('c.title', $qb->createNamedParameter('', IQueryBuilder::PARAM_STR)))
			->orderBy('d.id', 'ASC');
		$result = $qb->executeQuery();
		$hash = hash_init('sha256');
		$perPerson = [];
		$count = 0;
		while (($row = $result->fetch()) !== false) {
			$count++;
			$line = $row['id'] . ':' . $row['cluster_id'] . "\n";
			hash_update($hash, $line);
			$title = (string)$row['title'];
			if (!isset($perPerson[$title])) {
				$perPerson[$title] = hash_init('sha256');
			}
			hash_update($perPerson[$title], $line);
		}
		$result->closeCursor();

		$people = [];
		foreach ($perPerson as $title => $context) {
			$people[(string)$title] = substr(hash_final($context), 0, 32);
		}

		return [
			'count' => $count,
			'checksum' => substr(hash_final($hash), 0, 32),
			'people' => $people,
		];
	}

	/**
	 * Names and count of the user's people, so a rename is noticed.
	 *
	 * @return array{count: int, titles_hash: string}
	 */
	public function clusterSummary(string $userId): array {
		$qb = $this->db->getQueryBuilder();
		$qb->select('id', 'title')
			->from(self::CLUSTERS_TABLE)
			->where($qb->expr()->eq('user_id', $qb->createNamedParameter($userId, IQueryBuilder::PARAM_STR)))
			->orderBy('id', 'ASC');
		$result = $qb->executeQuery();
		$hash = hash_init('sha256');
		$count = 0;
		while (($row = $result->fetch()) !== false) {
			$title = (string)$row['title'];
			if ($title === '') {
				continue;
			}
			$count++;
			hash_update($hash, $row['id'] . ':' . $title . "\n");
		}
		$result->closeCursor();
		return ['count' => $count, 'titles_hash' => substr(hash_final($hash), 0, 32)];
	}

	/**
	 * Recognize background jobs that are reserved, meaning a worker has them.
	 *
	 * @return list<array{class: string, reserved_at: int}>
	 */
	public function runningRecognizeJobs(int $now): array {
		if (!$this->db->tableExists(self::JOBS_TABLE)) {
			return [];
		}
		$cutoff = $now - self::STALE_RESERVATION_SECONDS;
		$qb = $this->db->getQueryBuilder();
		$qb->select('class', 'reserved_at')
			->from(self::JOBS_TABLE)
			->where($qb->expr()->gt('reserved_at', $qb->createNamedParameter($cutoff, IQueryBuilder::PARAM_INT)));
		$result = $qb->executeQuery();
		$jobs = [];
		while (($row = $result->fetch()) !== false) {
			$class = (string)$row['class'];
			// Matched in PHP rather than with LIKE, because the class name is
			// full of backslashes and LIKE escaping differs between databases.
			if (str_starts_with($class, self::RECOGNIZE_JOB_PREFIX)) {
				$jobs[] = [
					'class' => $class,
					'reserved_at' => (int)$row['reserved_at'],
				];
			}
		}
		$result->closeCursor();
		return $jobs;
	}
}
