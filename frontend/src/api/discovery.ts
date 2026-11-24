import { apiClient } from './client';

export const uploadDiscoveryExport = (files: File[]) => {
  const fd = new FormData();
  files.forEach((f) => fd.append('files', f, f.name));
  return apiClient.post('/discovery/upload', fd, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
};
