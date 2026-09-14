<?php

declare(strict_types=1);
namespace OCA\DigikamFaceSync\Db;

use OCP\DB\QueryBuilder\IQueryBuilder;
use OCP\IDBConnection;

final class RecognizeRepository {
	private const DETECTIONS_TABLE = 'recognize_face_detections';
	private const CLUSTERS_TABLE = 'recognize_face_clusters';

	public function __construct(private IDBConnection $db) {
	}

	public function isAvailable(): bool {
		return $this->db->tableExists(self::DETECTIONS_TABLE)
			&& $this->db->tableExists(self::CLUSTERS_TABLE);
	}

	/** @return list<array<string, mixed>> */
	public function findDetections(int $fileId, string $userId): array {
		$qb = $this->db->getQueryBuilder();
		$qb->select('id', 'file_id', 'user_id', 'x', 'y', 'width', 'height', 'cluster_id', 'threshold')
			->from(self::DETECTIONS_TABLE)
			->where($qb->expr()->eq('file_id', $qb->createNamedParameter($fileId, IQueryBuilder::PARAM_INT)))
			->andWhere($qb->expr()->eq('user_id', $qb->createNamedParameter($userId, IQueryBuilder::PARAM_STR)));
		$result = $qb->executeQuery();
		$rows = $result->fetchAll();
		$result->closeCursor();
		return $rows;
	}

	/** @return array<string, mixed>|null */
	public function findDetection(int $detectionId, int $fileId, string $userId): ?array {
		$qb = $this->db->getQueryBuilder();
		$qb->select('id', 'file_id', 'user_id', 'x', 'y', 'width', 'height', 'cluster_id', 'threshold')
			->from(self::DETECTIONS_TABLE)
			->where($qb->expr()->eq('id', $qb->createNamedParameter($detectionId, IQueryBuilder::PARAM_INT)))
			->andWhere($qb->expr()->eq('file_id', $qb->createNamedParameter($fileId, IQueryBuilder::PARAM_INT)))
			->andWhere($qb->expr()->eq('user_id', $qb->createNamedParameter($userId, IQueryBuilder::PARAM_STR)))
			->setMaxResults(1);
		$result = $qb->executeQuery();
		$row = $result->fetch();
		$result->closeCursor();
		return is_array($row) ? $row : null;
	}

	public function assignDetectionCluster(
		int $detectionId,
		int $fileId,
		string $userId,
		int $clusterId,
	): void {
		$qb = $this->db->getQueryBuilder();
		$qb->update(self::DETECTIONS_TABLE)
			->set('cluster_id', $qb->createNamedParameter($clusterId, IQueryBuilder::PARAM_INT))
			->where($qb->expr()->eq('id', $qb->createNamedParameter($detectionId, IQueryBuilder::PARAM_INT)))
			->andWhere($qb->expr()->eq('file_id', $qb->createNamedParameter($fileId, IQueryBuilder::PARAM_INT)))
			->andWhere($qb->expr()->eq('user_id', $qb->createNamedParameter($userId, IQueryBuilder::PARAM_STR)));
		if ($qb->executeStatement() !== 1) {
			throw new \RuntimeException('Face detection changed before it could be assigned');
		}
	}

	/** @return array<string, mixed>|null */
	public function findClusterById(int $clusterId): ?array {
		$qb = $this->db->getQueryBuilder();
		$qb->select('id', 'title', 'user_id')
			->from(self::CLUSTERS_TABLE)
			->where($qb->expr()->eq('id', $qb->createNamedParameter($clusterId, IQueryBuilder::PARAM_INT)))
			->setMaxResults(1);
		$result = $qb->executeQuery();
		$row = $result->fetch();
		$result->closeCursor();
		return is_array($row) ? $row : null;
	}

	/** @return array<string, mixed>|null */
	public function findClusterByTitle(string $userId, string $title): ?array {
		$qb = $this->db->getQueryBuilder();
		$qb->select('id', 'title', 'user_id')
			->from(self::CLUSTERS_TABLE)
			->where($qb->expr()->eq('user_id', $qb->createNamedParameter($userId, IQueryBuilder::PARAM_STR)))
			->andWhere($qb->expr()->eq('title', $qb->createNamedParameter($title, IQueryBuilder::PARAM_STR)))
			->setMaxResults(1);
		$result = $qb->executeQuery();
		$row = $result->fetch();
		$result->closeCursor();
		return is_array($row) ? $row : null;
	}

	/** @return list<string> */
	public function findPeople(string $userId): array {
		$qb = $this->db->getQueryBuilder();
		$qb->select('title')
			->from(self::CLUSTERS_TABLE)
			->where($qb->expr()->eq('user_id', $qb->createNamedParameter($userId, IQueryBuilder::PARAM_STR)))
			->orderBy('title', 'ASC');
		$result = $qb->executeQuery();
		$people = [];
		while (($row = $result->fetch()) !== false) {
			$title = trim((string)$row['title']);
			if ($title !== '') {
				$people[] = $title;
			}
		}
		$result->closeCursor();
		return $people;
	}

	/** @return list<array<string, mixed>> */
	public function findNamedDetections(
		string $userId,
		?string $person,
		int $afterId,
		int $limit,
	): array {
		$qb = $this->db->getQueryBuilder();
		$qb->select(
			'd.id',
			'd.file_id',
			'd.x',
			'd.y',
			'd.width',
			'd.height',
			'd.cluster_id',
			'd.threshold',
			'c.title',
		)
			->from(self::DETECTIONS_TABLE, 'd')
			->innerJoin(
				'd',
				self::CLUSTERS_TABLE,
				'c',
				$qb->expr()->eq('d.cluster_id', 'c.id'),
			)
			->where($qb->expr()->eq('d.user_id', $qb->createNamedParameter($userId, IQueryBuilder::PARAM_STR)))
			->andWhere($qb->expr()->eq('c.user_id', $qb->createNamedParameter($userId, IQueryBuilder::PARAM_STR)))
			->andWhere($qb->expr()->gt('d.id', $qb->createNamedParameter($afterId, IQueryBuilder::PARAM_INT)))
			->orderBy('d.id', 'ASC')
			->setMaxResults($limit);
		if ($person !== null) {
			$qb->andWhere($qb->expr()->eq('c.title', $qb->createNamedParameter($person, IQueryBuilder::PARAM_STR)));
		}
		$result = $qb->executeQuery();
		$rows = $result->fetchAll();
		$result->closeCursor();
		return $rows;
	}

	public function getOrCreateCluster(string $userId, string $title): int {
		$existing = $this->findClusterByTitle($userId, $title);
		if ($existing !== null) {
			return (int)$existing['id'];
		}

		$qb = $this->db->getQueryBuilder();
		$qb->insert(self::CLUSTERS_TABLE)
			->values([
				'user_id' => $qb->createNamedParameter($userId, IQueryBuilder::PARAM_STR),
				'title' => $qb->createNamedParameter($title, IQueryBuilder::PARAM_STR),
			]);
		$qb->executeStatement();
		return (int)$qb->getLastInsertId();
	}

	/** @param list<float> $vector */
	public function insertDetection(
		string $userId,
		int $fileId,
		float $x,
		float $y,
		float $width,
		float $height,
		array $vector,
		int $clusterId,
	): int {
		$qb = $this->db->getQueryBuilder();
		$qb->insert(self::DETECTIONS_TABLE)
			->values([
				'user_id' => $qb->createNamedParameter($userId, IQueryBuilder::PARAM_STR),
				'file_id' => $qb->createNamedParameter($fileId, IQueryBuilder::PARAM_INT),
				'x' => $qb->createNamedParameter($x),
				'y' => $qb->createNamedParameter($y),
				'width' => $qb->createNamedParameter($width),
				'height' => $qb->createNamedParameter($height),
				// Recognize's FaceDetection::setVector() stores one feature row.
				'face_vector' => $qb->createNamedParameter(json_encode([$vector], JSON_THROW_ON_ERROR), IQueryBuilder::PARAM_STR),
				'cluster_id' => $qb->createNamedParameter($clusterId, IQueryBuilder::PARAM_INT),
				'threshold' => $qb->createNamedParameter(0.0),
			]);
		$qb->executeStatement();
		return (int)$qb->getLastInsertId();
	}
}
