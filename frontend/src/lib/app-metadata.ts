/** 暴露 renderer 可直接读取的构建期应用元数据。 */

import packageMetadata from '../../package.json';

export const APP_VERSION = packageMetadata.version;
