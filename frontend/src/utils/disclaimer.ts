/**
 * 「相似度不是涨停概率」这句口径在页面上只写一次。
 *
 * 后端那份同名的常量在 `backend/app/core/constants.py`，接口回传的 `disclaimer` 用的是它。
 * 顶栏那句不能等接口：健康检查都没回来时也必须挂着，所以前端自己留一份，
 * 但只留这一份——散在组件里写第二次，改口径就要挨个页面翻。
 */
export const SIMILARITY_DISCLAIMER = '相似度不是涨停概率'
