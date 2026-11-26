import { apiClient } from './client';

export const listTransformations = () =>
  apiClient.get('/workbench/transformations');
