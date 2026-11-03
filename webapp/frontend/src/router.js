import { createRouter, createWebHistory } from 'vue-router'
import ListPage from './views/ListPage.vue'
import ReaderPage from './views/ReaderPage.vue'
import InsightsPage from './views/InsightsPage.vue'
import FavoritesPage from './views/FavoritesPage.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'list', component: ListPage },
    { path: '/paper/:id', name: 'paper', component: ReaderPage },
    { path: '/insights', name: 'insights', component: InsightsPage },
    { path: '/favorites', name: 'favorites', component: FavoritesPage },
  ],
})
