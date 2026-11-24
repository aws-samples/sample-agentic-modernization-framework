import { apiClient } from './client';

export interface Skill {
  name: string;
  description: string;
  file: string;
  source: 'local';
}

export const listSkills = (): Promise<Skill[]> =>
  apiClient.get('/skills/');

export const getSkill = (name: string) =>
  apiClient.get(`/skills/${name}`);

export const uploadSkills = (files: File[]) => {
  const fd = new FormData();
  files.forEach((f) => fd.append('files', f, f.name));
  return apiClient.post('/skills/upload', fd, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
};
