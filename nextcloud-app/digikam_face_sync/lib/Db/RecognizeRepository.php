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
