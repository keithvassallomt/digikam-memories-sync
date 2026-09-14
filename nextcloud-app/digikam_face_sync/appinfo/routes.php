<?php

declare(strict_types=1);

return [
	'routes' => [
		['name' => 'face_import#capabilities', 'url' => '/api/v1/face-import', 'verb' => 'GET'],
		['name' => 'face_import#create', 'url' => '/api/v1/face-import', 'verb' => 'POST'],
		['name' => 'face_import#assign', 'url' => '/api/v1/face-assign', 'verb' => 'POST'],
		['name' => 'face_export#people', 'url' => '/api/v1/people', 'verb' => 'GET'],
		['name' => 'face_export#list', 'url' => '/api/v1/faces', 'verb' => 'GET'],
	],
];
