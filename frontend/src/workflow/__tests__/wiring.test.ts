import { describe, it, expect } from 'vitest';
import { wiringDefault, wiringFields, wiringPayload } from '../wiring';

const music = {
  detected_container_port: 5000, detected_health_check_path: '/', detected_build_dir: '.',
  detected_dynamodb_tables: [{ name: 'musicTable', hash_key: 'artist', env_var: 'DYNAMODB_TABLE' }],
};

describe('runtime wiring', () => {
  it('asks only for the fields the selected compute/DB need', () => {
    expect(wiringFields('ecs_fargate', 'dynamodb').map((f) => f.key)).toEqual(
      ['build_dir', 'container_port', 'health_check_path', 'dynamodb_table_name', 'dynamodb_hash_key']);
    expect(wiringFields('lambda', 'none').map((f) => f.key)).toEqual(['build_dir', 'lambda_handler', 'health_check_path']);
  });

  it('prefers the app value, then the repo scan', () => {
    expect(wiringDefault(music, 'container_port')).toBe(5000);
    expect(wiringDefault({ ...music, container_port: 8000 }, 'container_port')).toBe(8000);
    expect(wiringDefault(music, 'dynamodb_hash_key')).toBe('artist');
  });

  it('persists confirmed values (port as a number) but not unchanged app values', () => {
    const out = wiringPayload(music, { health_check_path: '/healthz' }, 'ecs_fargate', 'dynamodb');
    expect(out).toEqual({ build_dir: '.', container_port: 5000, health_check_path: '/healthz',
      dynamodb_table_name: 'musicTable', dynamodb_hash_key: 'artist' });
    expect(wiringPayload({ ...music, container_port: 5000 }, {}, 'ecs_fargate', 'none')).not.toHaveProperty('container_port');
  });
});
