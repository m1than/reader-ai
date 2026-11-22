// Test helper: loads a module inside a test body. While the module does not exist yet (Red), the test gets an empty
// object and fails on its first assertion instead of failing at collection.
export const load = async <T>(importer: () => Promise<T>): Promise<T> => {
  try {
    return await importer();
  } catch {
    return {} as T;
  }
};
