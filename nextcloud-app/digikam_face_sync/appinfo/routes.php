<?php

declare(strict_types=1);

return [
	'routes' => [
		['name' => 'face_import#capabilities', 'url' => '/api/v1/face-import', 'verb' => 'GET'],
		['name' => 'face_import#create', 'url' => '/api/v1/face-import', 'verb' => 'POST'],
	],
];
